"""Serverless functions for Schrödinger's Bus."""
import itertools
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from functools import cache

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from batch import is_valid_batch_id, parse_batch
from reliability import BOOTSTRAP_RESAMPLES, report
from version import code_version

# App setting holding the storage connection: the Function App's own account, which the experiments also used
# A separate account would keep test data apart from the platform's own use of storage
STORAGE = "AzureWebJobsStorage"
QUEUE = "batches-to-analyse"
BATCHES = "batches"  # Container holding the batch files
# Created once per Python process, which can outlive a host restart, so it isn't a machine count
INSTANCE_ID = uuid.uuid4().hex
# The machine's name, which counts instances, and can be checked against Application Insights
HOST_NAME = socket.gethostname()
# Worked out once at start-up, so results show which deployed code produced them
CODE_VERSION = code_version()
# Numbers each run on this instance, so 1 marks a cold start
# next() hands each run its own number, even when runs overlap on different threads
RUN_NUMBERS = itertools.count(1)

app = func.FunctionApp()


def utc_text(moment):
    """Return a UTC time as an ISO 8601 string, to the millisecond, so runs can be lined up."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@cache
def batch_container():
    """Return a client for the batches container, created on first use and shared by every run."""
    return BlobServiceClient.from_connection_string(os.environ[STORAGE]).get_container_client(BATCHES)


def json_response(body, status):
    """Return an HTTP response with a JSON body."""
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


# Called as PUT /api/batches/{batch_id}, with the batch as the request body
# Requires a function key, and the decorator below sends the queue message after the function returns
@app.function_name("receive_batch")
@app.route(route="batches/{batch_id}", methods=["PUT"], auth_level=func.AuthLevel.FUNCTION)
@app.queue_output(arg_name="message", queue_name=QUEUE, connection=STORAGE)
def receive_batch(req: func.HttpRequest, message: func.Out[str]) -> func.HttpResponse:
    """Function 1: validate a batch, save it, and queue it for analysis."""
    # The ID becomes the file name, so check it before anything is saved
    batch_id = req.route_params.get("batch_id")
    if not is_valid_batch_id(batch_id):
        return json_response({"error": "invalid batch ID"}, 400)
    body = req.get_body()
    try:
        polls = parse_batch(body.decode("utf-8"))
    except ValueError as error:  # UnicodeDecodeError is a ValueError too
        return json_response({"error": str(error)}, 400)

    # Saved here and finished before returning, rather than by an output binding
    # The host wrote binding outputs side by side, so Function 2 often got the message first and read an empty file
    # Overwriting keeps a repeated request harmless, and an upload error returns 500 with nothing queued
    batch_container().upload_blob(f"{batch_id}.jsonl", body, overwrite=True)
    message.set(batch_id)  # The message is just the ID, which tells Function 2 which file to read
    # 202, as analysis happens later in Function 2
    return json_response({"batch_id": batch_id, "polls": len(polls), "code_version": CODE_VERSION}, 202)


# {queueTrigger} is the message text, so Function 2 reads and writes files named after the batch ID
# Reprocessing the same message overwrites the same result, rather than creating a duplicate
@app.function_name("analyse_batch")
@app.queue_trigger(arg_name="msg", queue_name=QUEUE, connection=STORAGE)
@app.blob_input(arg_name="batch_file", path="batches/{queueTrigger}.jsonl", connection=STORAGE)
@app.blob_output(arg_name="result", path="results/{queueTrigger}.json", connection=STORAGE)
def analyse_batch(msg: func.QueueMessage, batch_file: str, result: func.Out[str]) -> None:
    """Function 2: runs automatically for each queue message. Failures retry, then go to the poison queue."""
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()  # Steadier than the wall clock for measuring runtime
    batch_id = msg.get_body().decode("utf-8")
    # Missing or empty file: raise so the message is retried, in case it hasn't been saved yet
    # Function 1 now saves the file before queueing, so this should only happen if something else went wrong
    if not batch_file:
        raise ValueError(f"no batch file for {batch_id}")

    run_number = next(RUN_NUMBERS)
    # An app setting, so experiments can change the workload without redeploying
    resamples = int(os.environ.get("BOOTSTRAP_RESAMPLES", BOOTSTRAP_RESAMPLES))
    # Seeded with the batch ID, so reprocessing a message gives identical measures
    summary = report(parse_batch(batch_file), resamples, batch_id)
    timing = {
        "code_version": CODE_VERSION,
        "instance_id": INSTANCE_ID,
        "host_name": HOST_NAME,
        "instance_run": run_number,
        "bootstrap_resamples": resamples,
        # Time from the message being queued to this run starting, including any retries
        "queue_delay_seconds": (started - msg.insertion_time).total_seconds(),
        "runtime_seconds": time.perf_counter() - clock,
        "dequeue_count": msg.dequeue_count,  # More than 1 means earlier attempts failed
        # Wall-clock times, so a run's total time can be measured from the first send to the last result
        "queued_at": utc_text(msg.insertion_time),
        "started_at": utc_text(started),
        "finished_at": utc_text(datetime.now(timezone.utc)),
    }
    result.set(json.dumps({"batch_id": batch_id, **summary, **timing}))
    # Also log the timing, so it can be queried in Application Insights
    logging.info("analysed %s", json.dumps({"batch_id": batch_id, **timing}))