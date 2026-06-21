#!/usr/bin/env python3
"""
Config parser for processRM pipeline.
Modeled after processMeerKAT's config_parser.py.
Reads INI-style configuration files and validates values.
"""

import argparse
import configparser
import ast
import os
import sys


def parse_args():
    """Parse command line arguments for config parser."""
    parser = argparse.ArgumentParser()
    parser.add_argument('-C', '--config', required=True,
                        help='Name of the input config file')
    args, __ = parser.parse_known_args()
    return vars(args)


def parse_config(filename):
    """
    Parse an INI config file. Returns nested dict keyed by section.
    Uses ast.literal_eval to interpret types (so strings need 'quotes').
    """
    if not os.path.exists(filename):
        raise FileNotFoundError(f"Config file not found: {filename}")

    config = configparser.ConfigParser(allow_no_value=True, inline_comment_prefixes=('#',))
    config.read(filename)

    taskvals = dict()
    for section in config.sections():
        if section not in taskvals:
            taskvals[section] = dict()

        for option in config.options(section):
            raw_value = config.get(section, option)
            if raw_value is None or raw_value.strip() == '':
                taskvals[section][option] = ''
                continue
            try:
                taskvals[section][option] = ast.literal_eval(raw_value)
            except (ValueError, SyntaxError):
                err = f"Cannot format field '{option}' in config file '{filename}', "
                err += f"currently set to: {raw_value}. Ensure strings are in 'quotes'."
                raise ValueError(err)

    return taskvals, config


def overwrite_config(filename, conf_dict={}, conf_sec='', sec_comment=''):
    """Write a section to the config file, creating or updating."""
    config_dict, config = parse_config(filename)

    if conf_sec not in config.sections():
        config.add_section(conf_sec)

    if sec_comment != '':
        config.set(conf_sec, sec_comment)

    for key in conf_dict.keys():
        config.set(conf_sec, key, str(conf_dict[key]))

    with open(filename, 'w') as f:
        config.write(f)


def validate_args(kwdict, section, key, dtype, default=None):
    """
    Validate a key in the config dict and coerce its type.
    If default is provided, returns default when key is missing.
    """
    if default is not None:
        val = kwdict.get(section, {}).pop(key, default)
    else:
        if section not in kwdict or key not in kwdict[section]:
            raise KeyError(f"Required config key '[{section}] {key}' is missing.")
        val = kwdict[section][key]

    if val == '' and default is not None:
        return default

    if dtype is str:
        try:
            val = str(val).rstrip('/ ')
        except UnicodeError:
            raise
    elif dtype is int:
        try:
            val = int(val)
        except (ValueError, TypeError):
            raise ValueError(f"Config '[{section}] {key}' must be an integer, got: {val}")
    elif dtype is float:
        try:
            val = float(val)
        except (ValueError, TypeError):
            raise ValueError(f"Config '[{section}] {key}' must be a float, got: {val}")
    elif dtype is bool:
        try:
            val = bool(val)
        except (ValueError, TypeError):
            raise
    elif dtype is list:
        if not isinstance(val, list):
            raise ValueError(f"Config '[{section}] {key}' must be a list, got: {type(val).__name__}")
    else:
        raise NotImplementedError('Only str, int, bool, float, and list are valid types.')

    return val


def validate_config(filename):
    """
    Validate the full config file structure and required fields.
    Returns the parsed config dict. Raises on critical errors.
    """
    taskvals, config = parse_config(filename)

    required_sections = ['data', 'chunking', 'rmsynth', 'rmclean', 'noise', 'slurm', 'merge', 'run']
    for sec in required_sections:
        if sec not in taskvals:
            raise ValueError(f"Config file missing required section: [{sec}]")

    # Validate [data] section
    fits_full = validate_args(taskvals, 'data', 'fits_full', str, default='')
    freqlist = validate_args(taskvals, 'data', 'freqlist', str, default='')

    if not fits_full:
        raise ValueError(
            "Config error: [data] fits_full is required (path to a full Stokes IQUV "
            "radio-continuum cube). Q/U-only inputs are no longer accepted."
        )

    if not freqlist:
        raise ValueError("Config error: [data] freqlist is required")

    # Validate [chunking] section
    parallel = validate_args(taskvals, 'chunking', 'parallel', int)
    chunks = validate_args(taskvals, 'chunking', 'chunks', int, default=parallel)

    if chunks > parallel:
        raise ValueError(
            f"Config error: [chunking] chunks ({chunks}) cannot exceed parallel ({parallel})"
        )
    if chunks < 1 or parallel < 1:
        raise ValueError("Config error: parallel and chunks must be >= 1")

    # Validate [rmclean] section — sanity check threshold/iterations
    # rmclean3d -c convention: positive = Jy/beam/RMSF, negative = N-sigma
    # (e.g. -c -5 means 5-sigma threshold). Both are valid; reject only 0.
    threshold = validate_args(taskvals, 'rmclean', 'threshold', float, default=1e-6)
    iterations = validate_args(taskvals, 'rmclean', 'iterations', int, default=5000)
    if threshold == 0:
        raise ValueError(f"Config error: [rmclean] threshold cannot be 0 "
                         f"(use positive Jy/beam/RMSF or negative N-sigma)")
    if iterations < 1:
        raise ValueError(f"Config error: [rmclean] iterations must be >= 1 (got {iterations})")

    return taskvals


if __name__ == '__main__':
    cliargs = parse_args()
    taskvals = validate_config(cliargs['config'])
    print("Config validated successfully.")
    print(taskvals)
