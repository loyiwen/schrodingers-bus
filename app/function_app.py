"""Serverless functions for Schrödinger's Bus."""
import json
import logging
import time
from datetime import datetime, timezone

import azure.functions as func

from batch import is_valid_batch_id, parse_batch, summarise

# App setting holding the storage connection, currently the Function App's own account
# TODO: decide whether experiments need a separate storage account, to keep test traffic apart
STORAGE = "AzureWebJobsStorage"
QUEUE = "batches-to-analyse"

app = func.FunctionApp()


def json_response(body, status):
    """Return an HTTP response with a JSON body."""
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


# Called as PUT /api/batches/{batch_id}, with the batch as the request body
# Requires a function key, and the decorators below save the outputs after the function returns
@app.function_name("receive_batch")
@app.route(route="batches/{batch_id}", methods=["PUT"], auth_level=func.AuthLevel.FUNCTION)
@app.blob_output(arg_name="batch_file", path="batches/{batch_id}.jsonl", connection=STORAGE)
@app.queue_output(arg_name="message", queue_name=QUEUE, connection=STORAGE)
def receive_batch(req: func.HttpRequest, batch_file: func.Out[str], message: func.Out[str]) -> func.HttpResponse:
    """Function 1: validate a batch, save it, and queue it for analysis."""
    # The ID becomes the file name, so check it before anything is saved
    batch_id = req.route_params.get("batch_id")
    if not is_valid_batch_id(batch_id):
        return json_response({"error": "invalid batch ID"}, 400)
    try:
        text = req.get_body().decode("utf-8")
        polls = parse_batch(text)
    except ValueError as error:  # UnicodeDecodeError is a ValueError too
        return json_response({"error": str(error)}, 400)

    # Nothing is saved or queued unless these are set
    batch_file.set(text)
    message.set(batch_id)  # The message is just the ID, which tells Function 2 which file to read
    # 202, as analysis happens later in Function 2
    return json_response({"batch_id": batch_id, "polls": len(polls)}, 202)


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
    # Missing file: raise so the message is retried, in case it hasn't been saved yet
    if batch_file is None:
        raise ValueError(f"no batch file for {batch_id}")

    summary = summarise(parse_batch(batch_file))
    timing = {
        # Time from the message being queued to this run starting, including any retries
        "queue_delay_seconds": (started - msg.insertion_time).total_seconds(),
        "runtime_seconds": time.perf_counter() - clock,
        "dequeue_count": msg.dequeue_count,  # More than 1 means earlier attempts failed
    }
    result.set(json.dumps({"batch_id": batch_id, **summary, **timing}))
    # Also log the timing, so it can be queried in Application Insights
    logging.info("analysed %s", json.dumps({"batch_id": batch_id, **timing}))