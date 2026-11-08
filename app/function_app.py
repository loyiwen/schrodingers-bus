"""Serverless functions for Schrödinger's Bus."""
import json

import azure.functions as func

from batch import is_valid_batch_id, parse_batch

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