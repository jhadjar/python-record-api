"""
This analysis is meant to run on the raw data and produce a JSON represenation of a minimal
inferred API.
"""
from __future__ import annotations

import collections
import operator
import os
import typing
import warnings
import itertools
import dataclasses
import warnings
import functools

import tqdm.std
import orjson
from typing_extensions import TypedDict

from . import jsonl
from .type_analysis import *
from .apis import *


INPUTS = os.environ["PYTHON_RECORD_API_INPUTS"]
OUTPUT = os.environ["PYTHON_RECORD_API_OUTPUT"]


def __main__():
    api = API()
    for name in INPUTS.split():
        api |= API.parse_file(name)
    res = api.json()
    with open(OUTPUT, "w") as o:
        o.write(res)


if __name__ == "__main__":
    __main__()
