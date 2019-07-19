# Copyright 2019 Cortex Labs, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import os
import json
import argparse
import traceback
import time
from flask import Flask, request, jsonify
from flask_api import status
from waitress import serve
import onnxruntime as rt
import numpy as np

from cortex.lib.storage import S3
from cortex import consts
from cortex.lib import util, package, Context
from cortex.lib.log import get_logger
from cortex.lib.exceptions import CortexException, UserRuntimeException, UserException

logger = get_logger()
logger.propagate = False  # prevent double logging (flask modifies root logger)

app = Flask(__name__)

onnx_to_np = {
    "tensor(float16)": "float16",
    "tensor(float)": "float32",
    "tensor(double)": "float64",
    "tensor(int32)": "int32",
    "tensor(int8)": "int8",
    "tensor(uint8)": "uint8",
    "tensor(int16)": "int16",
    "tensor(uint16)": "uint16",
    "tensor(int64)": "int64",
    "tensor(uint64)": "uint64",
    "tensor(bool)": "bool",
    "tensor(string)": "string",
}

local_cache = {
    "ctx": None,
    "api": None,
    "sess": None,
    "input_metadata": None,
    "output_metadata": None,
    "request_handler": None,
}


def prediction_failed(sample, reason=None):
    message = "prediction failed for sample: {}".format(json.dumps(sample))
    if reason:
        message += " ({})".format(reason)

    logger.error(message)
    return message, status.HTTP_406_NOT_ACCEPTABLE


@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"ok": True})


def transform_to_numpy(input_pyobj, input_metadata):
    target_dtype = onnx_to_np[input_metadata.type]
    target_shape = input_metadata.shape

    for idx, dim in enumerate(target_shape):
        if dim is None:
            target_shape[idx] = 1

    if type(input_pyobj) is not np.ndarray:
        np_arr = np.array(input_pyobj, dtype=target_dtype)
    else:
        np_arr = input_pyobj
    np_arr = np_arr.reshape(target_shape)
    return np_arr


def convert_to_onnx_input(sample, input_metadata_list):
    sess = local_cache["sess"]

    input_dict = {}
    if len(input_metadata_list) == 1:
        input_metadata = input_metadata_list[0]
        if util.is_dict(sample):
            if sample.get(input_metadata.name) is None:
                raise ValueError("sample should be a dict containing key: " + input_metadata.name)
            input_dict[input_metadata.name] = transform_to_numpy(
                sample[input_metadata.name], input_metadata
            )
        else:
            input_dict[input_metadata.name] = transform_to_numpy(sample, input_metadata)
    else:
        for input_metadata in input_metadata_list:
            if not sample.is_dict(input_metadata):
                expected_keys = [metadata.name for metadata in input_metadata_list]
                raise ValueError(
                    "sample should be a dict containing keys: " + ", ".join(expected_keys)
                )

            if sample.get(input_metadata.name) is None:
                raise ValueError("sample should be a dict containing key: " + input_metadata.name)

            input_dict[input_metadata.name] = transform_to_numpy(sample, input_metadata)
    return input_dict


@app.route("/<app_name>/<api_name>", methods=["POST"])
def predict(app_name, api_name):
    try:
        payload = request.get_json()
    except Exception as e:
        return "Malformed JSON", status.HTTP_400_BAD_REQUEST

    sess = local_cache["sess"]
    api = local_cache["api"]
    request_handler = local_cache.get("request_handler")
    input_metadata = local_cache["input_metadata"]
    output_metadata = local_cache["output_metadata"]

    response = {}

    if not util.is_dict(payload) or "samples" not in payload:
        util.log_pretty_flat(payload, logging_func=logger.error)
        return prediction_failed(payload, "top level `samples` key not found in request")

    logger.info("Predicting " + util.pluralize(len(payload["samples"]), "sample", "samples"))

    predictions = []
    samples = payload["samples"]
    if not util.is_list(samples):
        util.log_pretty_flat(samples, logging_func=logger.error)
        return prediction_failed(
            payload, "expected the value of key `samples` to be a list of json objects"
        )

    for i, sample in enumerate(payload["samples"]):
        util.log_indent("sample {}".format(i + 1), 2)
        try:
            util.log_indent("Raw sample:", indent=4)
            util.log_pretty_flat(sample, indent=6)

            if request_handler is not None and util.has_function(request_handler, "pre_inference"):
                sample = request_handler.pre_inference(sample, input_metadata)

            inference_input = convert_to_onnx_input(sample, input_metadata)
            model_outputs = sess.run([], inference_input)
            result = []
            for model_output in model_outputs:
                if type(model_output) is np.ndarray:
                    result.append(model_output.tolist())
                else:
                    result.append(model_output)

            if request_handler is not None and util.has_function(request_handler, "post_inference"):
                result = request_handler.post_inference(result, output_metadata)
            util.log_indent("Prediction:", indent=4)
            util.log_pretty_flat(result, indent=6)
            prediction = {"prediction": result}
        except CortexException as e:
            e.wrap("error", "sample {}".format(i + 1))
            logger.error(str(e))
            logger.exception(
                "An error occurred, see `cx logs -v api {}` for more details.".format(api["name"])
            )
            return prediction_failed(sample, str(e))
        except Exception as e:
            logger.exception(
                "An error occurred, see `cx logs -v api {}` for more details.".format(api["name"])
            )
            return prediction_failed(sample, str(e))

        predictions.append(prediction)

    response["predictions"] = predictions
    response["resource_id"] = api["id"]

    return jsonify(response)


def start(args):
    ctx = Context(s3_path=args.context, cache_dir=args.cache_dir, workload_id=args.workload_id)
    api = ctx.apis_id_map[args.api]

    local_cache["api"] = api
    local_cache["ctx"] = ctx
    if api.get("request_handler_impl_key") is not None:
        package.install_packages(ctx.python_packages, ctx.storage)
        local_cache["request_handler"] = ctx.get_request_handler_impl(api["name"])

    model_cache_path = os.path.join(args.model_dir, args.api)
    if not os.path.exists(model_cache_path):
        ctx.storage.download_file_external(api["model"], model_cache_path)

    sess = rt.InferenceSession(model_cache_path)
    local_cache["sess"] = sess
    local_cache["input_metadata"] = sess.get_inputs()
    local_cache["output_metadata"] = sess.get_outputs()
    logger.info("Serving model: {}".format(util.remove_resource_ref(api["model"])))
    serve(app, listen="*:{}".format(args.port))


def main():
    parser = argparse.ArgumentParser()
    na = parser.add_argument_group("required named arguments")
    na.add_argument("--workload-id", required=True, help="Workload ID")
    na.add_argument("--port", type=int, required=True, help="Port (on localhost) to use")
    na.add_argument(
        "--context",
        required=True,
        help="S3 path to context (e.g. s3://bucket/path/to/context.json)",
    )
    na.add_argument("--api", required=True, help="Resource id of api to serve")
    na.add_argument("--model-dir", required=True, help="Directory to download the model to")
    na.add_argument("--cache-dir", required=True, help="Local path for the context cache")
    parser.set_defaults(func=start)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()