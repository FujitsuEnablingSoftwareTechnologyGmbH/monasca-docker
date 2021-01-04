#!/usr/bin/env python

# Info: Monitoring plugin to check the Docker Hub Rate Limits
# License: MIT, Copyright (c) 2020-present GitLab B.V.
# Author: Michael Friedrich <mfriedrich@gitlab.com>

# From https://docs.docker.com/docker-hub/download-rate-limit/#how-can-i-check-my-current-rate
#
# > Remember that these headers are best-effort and there will be small variations.

# pip3 install -r requirements.txt
import sys
import os
import traceback
import requests
import json
from signal import signal, alarm, SIGALRM
from functools import partial
from argparse import ArgumentParser

VERSION = '2.0.0'

# Follows https://www.monitoring-plugins.org/doc/guidelines.html
STATES = { 0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN" }

def do_output(text, state=0,perfdata=None,name='Docker Hub'):
    if perfdata is None:
        perfdata = {}

    o = STATES.get(state) + ' - ' + name + ': ' + str(text)

    if perfdata:
        o += '|' + ' '.join(["'" + key + "'" + '=' + str(value) for key, value in perfdata.items()])

    print(o)
    sys.exit(state)

def print_headers(headers):
    print("HTTP Headers START")
    print('\r\n'.join('{}: {}'.format(k, v) for k, v in headers.items()),)
    print("HTTP Headers END")

def handle_sigalrm(signum, frame, timeout=None):
    do_output('Plugin timed out after %d seconds' % timeout, 3)

class DockerHub(object):
    def __init__(self, verbose, username, password):
        self.repository = 'ratelimitpreview/test'
        self.token_url = 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:' + self.repository + ':pull'
        self.registry_url = 'https://registry-1.docker.io/v2/' + self.repository + '/manifests/latest'
        self.username = username
        self.password = password

        self.verbose = verbose

    def do_verbose(self, text):
        if self.verbose:
            print("Notice: " + text)

    def limit_extractor(self, str_raw):
        self.do_verbose("Extracting limit from string: " + str(str_raw))

        if ";" in str_raw:
            split_arr = str_raw.split(';') # TODO: return other values too?
            if len(split_arr) > 0:
                return split_arr[0]
        else:
            return str_raw

    # Implements https://www.monitoring-plugins.org/doc/guidelines.html 
    def eval_thresholds(self, val, warn, crit):
        state = 0

        if warn:
            if float(val) < float(warn):
                state = 1

        if crit:
            if float(val) < float(crit):
                state = 2

        return state

    def get_token(self):
        # User has passed in own credentials, or we need anonymous access.
        if self.username and self.password:
            r_token = requests.get(self.token_url, auth=(self.username, self.password))

            self.do_verbose("Using Docker Hub credentials for '" + self.username + "'")
        else:
            r_token = requests.get(self.token_url)

            self.do_verbose("Using anonymous Docker Hub token")

        # error handling
        r_token.raise_for_status()

        resp_token = r_token.json()

        self.do_verbose("Response token:'" + json.dumps(resp_token) + "'")

        token = resp_token.get('token')

        if not token:
            raise Exception('Cannot obtain token from Docker Hub. Please try again!')

        return token

    ## Test against registry
    def get_registry_limits(self):
        headers_registry = { 'Authorization': 'Bearer ' + self.get_token() }

        # Use a HEAD request to fetch the headers and avoid a decreased pull count
        r_registry = requests.head(self.registry_url, headers=headers_registry)

        # error handling
        r_registry.raise_for_status()

        # We need to check the response headers!
        resp_headers = r_registry.headers

        if self.verbose:
            print_headers(resp_headers)

        limit = 0
        remaining = 0
        reset = 0

        if "RateLimit-Limit" in resp_headers and "RateLimit-Remaining" in resp_headers:
            limit = self.limit_extractor(resp_headers["RateLimit-Limit"])
            remaining = self.limit_extractor(resp_headers["RateLimit-Remaining"])

        if "RateLimit-Reset" in resp_headers:
            reset = self.limit_extractor(resp_headers["RateLimit-Reset"])

        return (limit, remaining, reset)

def main():
    parser = ArgumentParser(description="Version: %s" % (VERSION))

    parser.add_argument('-w', '--warning', type=int, default=80, help="warning threshold for remaining")
    parser.add_argument('-c', '--critical', type=int, default=20, help="critical threshold for remaining")
    parser.add_argument("-v", "--verbose", action="store_true", help="increase output verbosity")
    parser.add_argument("-t", "--timeout", help="Timeout in seconds (default 10s)", type=int, default=10)
    args = parser.parse_args(sys.argv[1:])

    verbose = args.verbose

    signal(SIGALRM, partial(handle_sigalrm, timeout=args.timeout))
    alarm(args.timeout)

    username = os.environ.get('DOCKERHUB_USERNAME')
    password = os.environ.get('DOCKERHUB_PASSWORD')

    dh = DockerHub(verbose, username, password)

    (limit, remaining, reset) = dh.get_registry_limits()

    if limit == 0 and remaining == 0:
        do_output('No limits found. You are safe and probably use a caching proxy already.', 0)
    else:
        state = dh.eval_thresholds(remaining, args.warning, args.critical)

        perfdata = {
            "limit": limit,
            "remaining": remaining,
            "reset": reset
        }

        do_output('Limit is %s remaining %s' % (limit, remaining), state, perfdata)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        do_output("Error: %s" % (e), 3)

