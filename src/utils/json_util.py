#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import os
import io
from tqdm import tqdm
from typing import List, Dict, Any
import numpy as np
from utils.logger_util import get_logger, setup_logging

logger = logging.getLogger("JsonUtil")


@get_logger
def read_question_view_json_file(logger, file_path: str) -> List[Dict[str, Any]]:
    with open(file_path, 'r', encoding='utf-8') as file:
        return json.load(file)


def default_serializer(obj):
    if isinstance(obj, np.float32):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


@get_logger
def write_question_view_json_file(logger, data: List[Dict[str, Any]], file_path: str):
    with open(file_path, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=4, ensure_ascii=False, default=default_serializer)


def _make_w_io_base(f, mode: str, encoding: str = 'utf-8'):
    """Create a writable file object, ensuring the directory exists."""
    if not isinstance(f, io.IOBase):
        f_dirname = os.path.dirname(f)
        if f_dirname != "":
            os.makedirs(f_dirname, exist_ok=True)
        f = open(f, mode=mode, encoding=encoding)
    return f


def _make_r_io_base(f, mode: str, encoding: str = 'utf-8'):
    """Create a readable file object."""
    if not isinstance(f, io.IOBase):
        f = open(f, mode=mode, encoding=encoding)
    return f


def jdump(obj, f, mode="w", indent=4, default=str, ensure_ascii=True, encoding="utf-8"):
    """Dump a str or dictionary to a file in JSON format.

    Args:
        obj: An object to be written.
        f: A string path to the location on disk.
        mode: Mode for opening the file.
        indent: Indent for storing JSON dictionaries.
        default: A function to handle non-serializable entries; defaults to `str`.
        ensure_ascii: Whether to encode strings as ASCII characters.
        encoding: The encoding type of file; defaults to `utf-8`.
    """
    f = _make_w_io_base(f, mode, encoding=encoding)
    if isinstance(obj, (dict, list)):
        json.dump(obj, f, indent=indent, default=default, ensure_ascii=ensure_ascii)
    elif isinstance(obj, str):
        f.write(obj)
    else:
        raise ValueError(f"Unexpected type: {type(obj)}")
    f.close()


def jload(f, mode="r", encoding="utf-8"):
    """Load a .json file into a dictionary.

    Args:
        f: A string path or file object.
        mode: Mode for opening the file.
        encoding: The encoding type of file.
    Returns:
        A JSON dictionary.
    """
    f = _make_r_io_base(f, mode, encoding=encoding)
    jdict = json.load(f)
    f.close()
    return jdict


def select_n_data(input_file, output_file, n_data):
    """Select the first n_data entries from input file and save to output file."""
    json = jload(input_file)
    sub_json = []
    for i, sample in tqdm(enumerate(iter(json))):
        if i >= n_data:
            break
        sub_json.append(sample)
    jdump(sub_json, output_file, ensure_ascii=False, encoding="utf-8")


def jsonl_2_json(jsonl_file, json_file):
    """Convert a JSONL file to a JSON list file."""
    with open(jsonl_file, 'r', encoding="utf-8") as infile:
        jsonl_data = infile.readlines()

    json_data = [json.loads(line) for line in jsonl_data]
    jdump(json_data, json_file, ensure_ascii=False, encoding="utf-8")


def jsonline_dump(jsonl_data, output_file, ensure_ascii=False, encoding="utf-8"):
    """Write JSON Lines data to a file.

    Args:
        jsonl_data (list): List of JSON objects.
        output_file (str): Output file path.
        ensure_ascii (bool): Whether to escape non-ASCII characters. Default: False.
        encoding (str): Output file encoding. Default: "utf-8".
    """
    with open(output_file, 'a', encoding=encoding) as outfile:
        for line in jsonl_data:
            outfile.write(json.dumps(line, ensure_ascii=ensure_ascii) + "\n")


def jsonline_load(json_file, encoding="utf-8"):
    """Load data from a JSON Lines file and return a list of JSON objects.

    Args:
        json_file (str): Path to the JSON Lines file.
        encoding (str): File encoding. Default: "utf-8".
    Returns:
        list: List of JSON objects loaded from the file.
    """
    jsonl_data = []
    with open(json_file, 'r', encoding=encoding) as infile:
        str_data = infile.readlines()
    for element in str_data:
        jsonl_data.append(json.loads(element))
    return jsonl_data


def read_txt_file(path):
    f = _make_w_io_base(path, "r", encoding="utf-8")
    content = f.read()
    f.close()
    return content
