#!/usr/bin/env python

# (C) Copyright 2017 Hewlett Packard Enterprise Development LP
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#


import argparse
import datetime
import gzip
import json
import logging
import os
import re
import shutil
import signal
import six
import subprocess
import sys
import time
import yaml

from google.oauth2 import service_account
from google.cloud import storage

#logging.basicConfig(format = '%(asctime)s %(levelname).5s [%(name)s] [%(lineno)4d]  %(message)s [%(funcName)s] ')
logging.basicConfig(format = '%(asctime)s %(levelname).5s %(message)s')
LOG=logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

parser = argparse.ArgumentParser(description='CI command')
parser.add_argument('-p', '--pipeline', dest='pipeline', default=None, required=True,
                    help='Select the pipeline [metrics|logs]')
parser.add_argument('-nv', '--non-voting', dest='non_voting', action='store_true',
                    help='Set the check as non-voting')
parser.add_argument('-pl', '--print-logs', dest='print_logs', action='store_true',
                    help='Print containers logs')
args = parser.parse_args()
LOG.debug(args)

TAG_REGEX = re.compile(r'^!(\w+)(?:\s+([\w-]+))?$')

METRIC_PIPELINE_MARKER = 'metrics'
LOG_PIPELINE_MARKER = 'logs'

METRIC_PIPELINE_MODULE_TO_COMPOSE_SERVICES = {
    'monasca-agent-forwarder': 'agent-forwarder',
    'zookeeper': 'zookeeper',
    'influxdb': 'influxdb',
    'kafka': 'kafka',
    'kafka-init': 'kafka-init',
    'monasca-thresh': 'thresh',
    'monasca-persister-python': 'monasca-persister',
    'mysql-init': 'mysql-init',
    'monasca-api-python': 'monasca',
    'influxdb-init': 'influxdb-init',
    'monasca-agent-collector': 'agent-collector',
    'grafana': 'grafana',
    'monasca-notification': 'monasca-notification',
    'grafana-init': 'grafana-init',
    'monasca-statsd': 'monasca-statsd'
}
LOGS_PIPELINE_MODULE_TO_COMPOSE_SERVICES = {
    'monasca-log-metrics': 'log-metrics',
    'monasca-log-persister': 'log-persister',
    'monasca-log-transformer': 'log-transformer',
    'elasticsearch-curator': 'elasticsearch-curator',
    'elasticsearch-init': 'elasticsearch-init',
    'kafka-init': 'kafka-log-init',
    'kibana': 'kibana',
    'monasca-log-api': 'log-api',
    'monasca-log-agent': 'log-agent',
    'logspout': 'logspout',
}

METRIC_PIPELINE_INIT_JOBS = ('influxdb-init', 'kafka-init', 'mysql-init', 'grafana-init')
LOG_PIPELINE_INIT_JOBS = ('elasticsearch-init', 'kafka-log-init')
INIT_JOBS = {
    METRIC_PIPELINE_MARKER: METRIC_PIPELINE_INIT_JOBS,
    LOG_PIPELINE_MARKER: LOG_PIPELINE_INIT_JOBS
}

METRIC_PIPELINE_SERVICES = METRIC_PIPELINE_MODULE_TO_COMPOSE_SERVICES.values()
"""Explicit list of services for docker compose
to launch for metrics pipeline"""
LOG_PIPELINE_SERVICES = (['kafka'] +
                         LOGS_PIPELINE_MODULE_TO_COMPOSE_SERVICES.values())
"""Explicit list of services for docker compose
to launch for logs pipeline"""

PIPELINE_TO_YAML_COMPOSE = {
    METRIC_PIPELINE_MARKER: 'docker-compose-metric.yml',
    LOG_PIPELINE_MARKER: 'docker-compose-log.yml'
}

CI_COMPOSE_FILE = 'ci-compose.yml'

LOG_DIR = 'monasca-logs/' + \
          datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
BUILD_LOG_DIR = LOG_DIR + '/build/'
RUN_LOG_DIR = LOG_DIR + '/run/'
LOG_DIRS = [LOG_DIR, BUILD_LOG_DIR, RUN_LOG_DIR]
MAX_RAW_LOG_SIZE = 1024L  # 1KiB


class SubprocessException(Exception):
    pass


class FileReadException(Exception):
    pass


class FileWriteException(Exception):
    pass


class InitJobFailedException(Exception):
    pass


class TempestTestFailedException(Exception):
    pass


class SmokeTestFailedException(Exception):
    pass


def print_logs():
    for log_dir in LOG_DIRS:
        for file_name in os.listdir(log_dir):
            file_path = log_dir + file_name
            if os.path.isfile(file_path):
                with open(file_path, 'r') as f:
                    log_contents = f.read()
                    LOG.info("#" * 100)
                    LOG.info("###### Container Logs from {0}".format(file_name))
                    LOG.info("#" * 100)
                    LOG.info(log_contents)

def get_client():
    LOG.info('get_client() BEGIN')
    cred_dict_str = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', None)
    if not cred_dict_str:
        LOG.warn('Could not found GCP credentials in environment variables')
        return None

    cred_dict = json.loads(cred_dict_str)
    try:
        credentials = service_account.Credentials.from_service_account_info(cred_dict)
        return storage.Client(credentials=credentials, project='monasca-ci-logs')
    except Exception as e:
        LOG.error('Unexpected error getting GCP credentials: {}'.format(e))
        return None


def upload_log_files(printlogs):
    LOG.info('upload_log_files() BEGIN')
    client = get_client()
    if not client:
        LOG.warn('Could not upload logs to GCP.')
        if printlogs:
            return print_logs()
        else:
            return
    bucket = client.bucket('monasca-ci-logs')

    uploaded_files = {}
    for log_dir in LOG_DIRS:
        uploaded_files.update(upload_files(log_dir, bucket))

    return uploaded_files


def upload_manifest(pipeline, voting, uploaded_files, dirty_modules, files, tags):
    LOG.info('upload_manifest() BEGIN')
    client = get_client()
    if not client:
        LOG.warn('Could not upload logs to GCP')
        return
    bucket = client.bucket('monasca-ci-logs')

    manifest_dict = print_env(pipeline, voting, to_print=False)
    manifest_dict['modules'] = {}
    for module in dirty_modules:
        manifest_dict['modules'][module] =  {'files': []}
        for f in files:
            if module in f:
                if 'init' not in module and 'init' not in f or 'init' in module and 'init' in f:
                    manifest_dict['modules'][module]['files'].append(f)

        manifest_dict['modules'][module]['uploaded_log_file'] = {}
        for f, url in uploaded_files.iteritems():
            if module in f:
                if 'init' not in module and 'init' not in f or 'init' in module and 'init' in f:
                    manifest_dict['modules'][module]['uploaded_log_file'][f] =  url

    manifest_dict['run_logs'] = {}
    for f, url in uploaded_files.iteritems():
        if 'run' in f:
            manifest_dict['run_logs'][f] = url
    manifest_dict['tags'] = tags

    file_path = LOG_DIR + 'manifest.json'
    upload_file(bucket, file_path, file_str=json.dumps(manifest_dict, indent=2),
                content_type='application/json')


def upload_files(log_dir, bucket):
    LOG.info('upload_files() BEGIN')
    uploaded_files = {}
    blob = bucket.blob(log_dir)
    for f in os.listdir(log_dir):
        file_path = log_dir + f
        if os.path.isfile(file_path):
            if os.stat(file_path).st_size > MAX_RAW_LOG_SIZE:
                with gzip.open(file_path + '.gz', 'w') as f_out, open(file_path, 'r') as f_in:
                    shutil.copyfileobj(f_in, f_out)
                file_path += '.gz'
                url = upload_file(bucket, file_path, content_encoding='gzip')
            else:
                url = upload_file(bucket, file_path)
            uploaded_files[file_path] = url
    return uploaded_files


def upload_file(bucket, file_path, file_str=None, content_type='text/plain',
                content_encoding=None):
    LOG.info('upload_file() BEGIN')
    try:
        blob = bucket.blob(file_path)
        if content_encoding:
            blob.content_encoding = content_encoding
        if file_str:
            blob.upload_from_string(file_str, content_type=content_type)
        else:
            blob.upload_from_filename(file_path, content_type=content_type)
        blob.make_public()

        url = blob.public_url
        if isinstance(url, six.binary_type):
            url = url.decode('utf-8')

        LOG.info('Public url for log: {}'.format(url))
        return url
    except Exception as e:
        LOG.error('Unexpected error uploading log files to {}'
               'Skipping upload. Got: {}'.format(file_path, e))
        if content_encoding == 'gzip':
            f = gzip.open(file_path, 'r')
        else:
            f = open(file_path, 'r')
        log_contents = f.read()
        LOG.error(log_contents)
        f.close()


def set_log_dir():
    try:
        LOG.debug('Working directory: {0}'.format(os.getcwd()))
        if not os.path.exists(LOG_DIR):
            LOG.debug('Creating LOG_DIR: {0}'.format(LOG_DIR))
            os.makedirs(LOG_DIR)
        if not os.path.exists(BUILD_LOG_DIR):
            LOG.debug('Creating BUILD_LOG_DIR: {0}'.format(BUILD_LOG_DIR))
            os.makedirs(BUILD_LOG_DIR)
        if not os.path.exists(RUN_LOG_DIR):
            LOG.debug('Creating RUN_LOG_DIR: {0}'.format(RUN_LOG_DIR))
            os.makedirs(RUN_LOG_DIR)
    except Exception as e:
        LOG.error('Unexpected error {0}'.format(e))


def get_changed_files():
    LOG.info('get_changed_files() BEGIN')
    commit_range = os.environ.get('TRAVIS_COMMIT_RANGE', None)
    if not commit_range:
        return []

    p = subprocess.Popen([
        'git', 'diff', '--name-only', commit_range
    ], stdout=subprocess.PIPE)

    stdout, _ = p.communicate()
    if p.returncode != 0:
        raise SubprocessException('git returned non-zero exit code')

    return [line.strip() for line in stdout.splitlines()]


def get_message_tags():
    LOG.info('get_message_tags() BEGIN')
    commit = os.environ.get('TRAVIS_COMMIT_RANGE', None)
    if not commit:
        return []

    p = subprocess.Popen([
        'git', 'log', '--pretty=%B', '-1', commit
    ], stdout=subprocess.PIPE)
    stdout, _ = p.communicate()
    if p.returncode != 0:
        raise SubprocessException('git returned non-zero exit code')

    tags = []
    for line in stdout.splitlines():
        line = line.strip()
        m = TAG_REGEX.match(line)
        if m:
            tags.append(m.groups())

    return tags


def get_dirty_modules(dirty_files):
    LOG.info('get_dirty_modules() BEGIN')
    dirty = set()
    for f in dirty_files:
        if os.path.sep in f:
            mod, _ = f.split(os.path.sep, 1)

            if not os.path.exists(os.path.join(mod, 'Dockerfile')):
                continue

            if not os.path.exists(os.path.join(mod, 'build.yml')):
                continue

            dirty.add(mod)

#    if len(dirty) > 5:
#        LOG.error('Max number of changed modules exceded.',
#                  'Please break up the patch set until a maximum of 5 modules are changed.')
#        sys.exit(1)
    return list(dirty)


def get_dirty_for_module(files, module=None):
    LOG.info('get_dirty_for_module() BEGIN')
    ret = []
    for f in files:
        if os.path.sep in f:
            mod, rel_path = f.split(os.path.sep, 1)
            if mod == module:
                ret.append(rel_path)
        else:
            # top-level file, no module
            if module is None:
                ret.append(f)

    return ret


def run_build(modules):
    LOG.info('run_build() BEGIN')
    log_dir = BUILD_LOG_DIR
    build_args = ['dbuild', '-sd', '--build-log-dir', log_dir, 'build', 'all', '+', ':ci-cd'] + modules
    LOG.info('build command:', build_args)

    p = subprocess.Popen(build_args)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if p.wait() != 0:
        LOG.error('build failed, exiting!')
        sys.exit(p.returncode)


def run_push(modules, pipeline):
    LOG.info('run_push(modules) BEGIN')

    if pipeline == 'logs':
        LOG.info('images are already pushed by metrics-pipeline, skipping!')
        return

    if os.environ.get('TRAVIS_SECURE_ENV_VARS', None) != "true":
        LOG.info('No push permissions in this context, skipping!')
        LOG.info('Not pushing: %r' % modules)
        return

    username = os.environ.get('DOCKER_HUB_USERNAME', None)
    password = os.environ.get('DOCKER_HUB_PASSWORD', None)
    #LOG.info('run_push(modules) username', username)
    #LOG.info('run_push(modules) password', password)

    if username and password:
        LOG.info('Logging into docker registry...')
        login = subprocess.Popen([
            'docker', 'login',
            '-u', username,
            '--password-stdin'
        ], stdin=subprocess.PIPE)
        login.communicate(password)
        if login.returncode != 0:
            LOG.error('Docker registry login failed, cannot push!')
            sys.exit(1)

    log_dir = BUILD_LOG_DIR
    push_args = ['dbuild', '-sd', '--build-log-dir', log_dir, 'build', 'push', 'all'] + modules
    LOG.info('push command:', push_args)

    p = subprocess.Popen(push_args)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if p.wait() != 0:
        print('build failed, exiting!')
        sys.exit(p.returncode)


def run_readme(modules):
    LOG.info('run_readme() BEGIN')
    if os.environ.get('TRAVIS_SECURE_ENV_VARS', None) != "true":
        LOG.info('No Docker Hub permissions in this context, skipping!')
        LOG.info('Not updating READMEs: %r' % modules)
        return

    log_dir = BUILD_LOG_DIR
    readme_args = ['dbuild', '-sd', '--build-log-dir', log_dir, 'readme'] + modules
    LOG.info('readme command:', readme_args)

    p = subprocess.Popen(readme_args)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if p.wait() != 0:
        LOG.error('build failed, exiting!')
        sys.exit(p.returncode)


def update_docker_compose(modules, pipeline):
    LOG.info('update_docker_compose() BEGIN')
    compose_dict = load_yml(PIPELINE_TO_YAML_COMPOSE['metrics'])
    services_to_changes = METRIC_PIPELINE_MODULE_TO_COMPOSE_SERVICES.copy()

    if pipeline == 'logs':
        LOG.info('\'logs\' pipeline is enabled, including in CI run')
        log_compose = load_yml(PIPELINE_TO_YAML_COMPOSE['logs'])
        compose_dict['services'].update(log_compose['services'])
        services_to_changes.update(
            LOGS_PIPELINE_MODULE_TO_COMPOSE_SERVICES.copy()
        )

    if modules:
        compose_services = compose_dict['services']
        for module in modules:
            # Not all modules are included in docker compose
            if module not in services_to_changes:
                continue
            service_name = services_to_changes[module]
            services_to_update = service_name.split(',')
            for service in services_to_update:
                image = compose_services[service]['image']
                image = image.split(':')[0]
                image += ":ci-cd"
                compose_services[service]['image'] = image

    # Update compose version
    compose_dict['version'] = '2'

    try:
        with open(CI_COMPOSE_FILE, 'w') as docker_compose:
            yaml.dump(compose_dict, docker_compose, default_flow_style=False)
    except:
        raise FileWriteException(
            'Error writing CI dictionary to %s' % CI_COMPOSE_FILE
        )


def load_yml(yml_path):
    LOG.info('load_yml() BEGIN')
    try:
        with open(yml_path) as compose_file:
            compose_dict = yaml.safe_load(compose_file)
            return compose_dict
    except:
        raise FileReadException('Failed to read %s', yml_path)


def handle_pull_request(files, modules, tags, pipeline):
    LOG.info('handle_pull_request() BEGIN')
    modules_to_build = modules[:]

    for tag, arg in tags:
        if tag in ('build', 'push'):
            if arg is None:
                # arg-less doesn't make sense for PRs since any changes to a
                # module already result in a rebuild
                continue

            modules_to_build.append(arg)

    # note(kornicameister) check if module belong to the pipeline
    # if not, there's no point of building that as it will be build
    # for the given pipeline
    pipeline_modules = pick_modules_for_pipeline(modules_to_build, pipeline)

    if pipeline_modules:
        run_build(pipeline_modules)
    else:
        LOG.info('No modules to build.')

    update_docker_compose(pipeline_modules, pipeline)
    run_docker_keystone()
    run_docker_compose(pipeline)
    wait_for_init_jobs(pipeline)
    LOG.info('Waiting for containers to be ready 1 min...')
    time.sleep(60)
    output_docker_ps()

    cool_test_mapper = {
        'smoke': {
            METRIC_PIPELINE_MARKER: run_smoke_tests_metrics,
            LOG_PIPELINE_MARKER: lambda : LOG.info('No smoke tests for logs')
        },
        'tempest': {
            METRIC_PIPELINE_MARKER: run_tempest_tests_metrics,
            LOG_PIPELINE_MARKER: lambda : LOG.info('No tempest tests for logs')
        }
    }

    cool_test_mapper['smoke'][pipeline]()
    cool_test_mapper['tempest'][pipeline]()


def pick_modules_for_pipeline(modules, pipeline):
    LOG.info('pick_modules_for_pipeline() BEGIN')
    if not modules:
        return []

    modules_for_pipeline = {
        LOG_PIPELINE_MARKER: LOGS_PIPELINE_MODULE_TO_COMPOSE_SERVICES.keys(),
        METRIC_PIPELINE_MARKER: METRIC_PIPELINE_MODULE_TO_COMPOSE_SERVICES.keys()
    }

    pipeline_modules = modules_for_pipeline[pipeline]

    # some of the modules are not used in pipelines, but should be
    # taken into consideration during the build
    other_modules = [
        'storm'
    ]
    LOG.info('modules: %s \n pipeline_modules: %s' % (modules, pipeline_modules))

    # iterate over copy of all modules that are planned for the build
    # if one of them does not belong to active pipeline
    # remove from current run
    for m in modules[::]:
        if m not in pipeline_modules:
            if m in other_modules:
                LOG.info('%s is not part of either pipeline, but it will be build anyway' % m)
                continue
            LOG.info('Module %s does not belong to %s, skipping' % (
                m, pipeline
            ))
            modules.remove(m)

    return modules


def get_current_init_status(docker_id):
    LOG.info('get_current_init_status() BEGIN')
    init_status = ['docker', 'inspect', '-f', '{{ .State.ExitCode }}:{{ .State.Status }}', docker_id]
    p = subprocess.Popen(init_status, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)

    output, err = p.communicate()

    if p.wait() != 0:
        LOG.info('getting current status failed')
        return False
    status_output = output.rstrip()

    exit_code, status = status_output.split(":", 1)
    return exit_code == "0" and status == "exited"


def output_docker_logs():
    LOG.info('output_docker_logs() BEGIN')
    docker_names = ['docker', 'ps', '-a', '--format', '"{{.Names}}"']

    p = subprocess.Popen(docker_names, stdout=subprocess.PIPE)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)

    output, err = p.communicate()
    names = output.replace('"', '').split('\n')

    for name in names:
        if not name:
            continue

        docker_logs = ['docker', 'logs', '-t', name]
        log_name = RUN_LOG_DIR + 'docker_log_' + name + '.log'
        with open(log_name, 'w') as out:
            p = subprocess.Popen(docker_logs, stdout=out,
                                 stderr=subprocess.STDOUT)
        signal.signal(signal.SIGINT, kill)
        if p.wait() != 0:
            LOG.error('Error running docker log for {}'.format(name))


def output_docker_ps():
    LOG.info('output_docker_ps() BEGIN')
    docker_ps = ['docker', 'ps', '-a']

    docker_ps_process = subprocess.Popen(docker_ps)

    def kill(signal, frame):
        docker_ps_process.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if docker_ps_process.wait() != 0:
        LOG.error('Error running docker ps')


def output_compose_details(pipeline):
    LOG.info('output_compose_details() BEGIN')
    LOG.info('Running docker-compose -f {0}'.format(CI_COMPOSE_FILE))
    if pipeline == 'metrics':
        services = METRIC_PIPELINE_SERVICES
    else:
        services = LOG_PIPELINE_SERVICES
    LOG.info('All services that are about to start: ', services)


def get_docker_id(init_job):
    LOG.info('get_docker_id() BEGIN')
    docker_id = ['docker-compose',
                 '-f', CI_COMPOSE_FILE,
                 'ps',
                 '-q', init_job]

    p = subprocess.Popen(docker_id, stdout=subprocess.PIPE)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)

    output, err = p.communicate()

    if p.wait() != 0:
        LOG.error('error getting docker id')
        return ""
    return output.rstrip()


def wait_for_init_jobs(pipeline):
    LOG.info('wait_for_init_jobs() BEGIN')
    init_status_dict = {job: False for job in INIT_JOBS[pipeline]}
    docker_id_dict = {job: "" for job in INIT_JOBS[pipeline]}

    amount_succeeded = 0
    for attempt in range(40):
        time.sleep(30)
        amount_succeeded = 0
        for init_job, status in init_status_dict.items():
            if docker_id_dict[init_job] == "":
                docker_id_dict[init_job] = get_docker_id(init_job)
            if status:
                amount_succeeded += 1
            else:
                updated_status = get_current_init_status(docker_id_dict[init_job])
                init_status_dict[init_job] = updated_status
                if updated_status:
                    amount_succeeded += 1
        if amount_succeeded == len(docker_id_dict):
            LOG.info("All init-jobs passed!")
            break
        else:
            LOG.info("Not all init jobs have succeeded. Attempt: " + str(attempt + 1) + " of 40")

    if amount_succeeded != len(docker_id_dict):
        LOG.error("Init-jobs did not succeed, printing docker ps and logs")
        raise InitJobFailedException()


def handle_push(files, modules, tags, pipeline):
    LOG.info('handle_push() BEGIN')
    modules_to_push = []
    modules_to_readme = []

    force_push = False
    force_readme = False

    for tag, arg in tags:
        if tag in ('build', 'push'):
            if arg is None:
                force_push = True
            else:
                modules_to_push.append(arg)
        elif tag == 'readme':
            if arg is None:
                force_readme = True
            else:
                modules_to_readme.append(arg)

    for module in modules:
        dirty = get_dirty_for_module(files, module)
        if force_push or 'build.yml' in dirty:
            modules_to_push.append(module)

        if force_readme or 'README.md' in dirty:
            modules_to_readme.append(module)

    if modules_to_push:
        run_push(modules_to_push, pipeline)
    else:
        LOG.info('No modules to push.')

    if modules_to_readme:
        run_readme(modules_to_readme)
    else:
        LOG.info('No READMEs to update.')

def run_docker_keystone():
    LOG.info('Running docker compose for Keystone')

    username = os.environ.get('DOCKER_HUB_USERNAME', None)
    password = os.environ.get('DOCKER_HUB_PASSWORD', None)

    if username and password:
        LOG.info('Logging into docker registry...')
        login = subprocess.Popen([
            'docker', 'login',
            '-u', username,
            '--password-stdin'
        ], stdin=subprocess.PIPE)
        login.communicate(password)
        if login.returncode != 0:
            LOG.error('Docker registry login failed!')
            sys.exit(1)


    docker_compose_dev_command = ['docker-compose',
                              '-f', 'docker-compose-dev.yml',
                              'up', '-d']

    with open(RUN_LOG_DIR + 'docker_compose_dev.log', 'w') as out:
        p = subprocess.Popen(docker_compose_dev_command, stdout=out)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if p.wait() != 0:
        LOG.error('docker compose failed, exiting!')
        sys.exit(p.returncode)

    # print out running images for debugging purposes
    LOG.info('docker compose dev succeeded')
    output_docker_ps()


def run_docker_compose(pipeline):
    LOG.info('Running docker compose')
    output_compose_details(pipeline)

    username = os.environ.get('DOCKER_HUB_USERNAME', None)
    password = os.environ.get('DOCKER_HUB_PASSWORD', None)

    if username and password:
        LOG.info('Logging into docker registry...')
        login = subprocess.Popen([
            'docker', 'login',
            '-u', username,
            '--password-stdin'
        ], stdin=subprocess.PIPE)
        login.communicate(password)
        if login.returncode != 0:
            LOG.error('Docker registry login failed!')
            sys.exit(1)

    if pipeline == 'metrics':
        services = METRIC_PIPELINE_SERVICES
    else:
        services = LOG_PIPELINE_SERVICES

    docker_compose_command = ['docker-compose',
                              '-f', CI_COMPOSE_FILE,
                              'up', '-d'] + services

    with open(RUN_LOG_DIR + 'docker_compose.log', 'w') as out:
        p = subprocess.Popen(docker_compose_command, stdout=out)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if p.wait() != 0:
        LOG.error('docker compose failed, exiting!')
        sys.exit(p.returncode)

    # print out running images for debugging purposes
    LOG.info('docker compose succeeded')
    output_docker_ps()


def run_smoke_tests_metrics():
    LOG.info('Running Smoke-tests')
    #TODO: branch as variable... use TRAVIS_PULL_REQUEST_BRANCH ?
    smoke_tests_run = ['docker', 'run',
                       '-e', 'OS_AUTH_URL=http://keystone:35357/v3',
                       '-e', 'MONASCA_URL=http://monasca:8070',
                       '-e', 'METRIC_NAME_TO_CHECK=monasca.thread_count',
                       '--net', 'monasca-docker_default',
                       '-p', '0.0.0.0:8080:8080',
                       '--name', 'monasca-docker-smoke',
                       'fest/smoke-tests:pike-latest']
    #TODO: repo has no stable/pike!

    p = subprocess.Popen(smoke_tests_run)

    def kill(signal, frame):
        p.kill()
        print()
        print('killed!')
        sys.exit(1)

    signal.signal(signal.SIGINT, kill)
    if p.wait() != 0:
        LOG.error('Smoke-tests failed, listing containers/logs.')
        raise SmokeTestFailedException()


def run_tempest_tests_metrics():
    LOG.info('Running Tempest-tests')
    tempest_tests_run = ['docker', 'run',
                         '-e', 'KEYSTONE_IDENTITY_URI=http://keystone:35357',
                         '-e', 'OS_AUTH_URL=http://keystone:35357/v3',
                         '-e', 'MONASCA_WAIT_FOR_API=true',
                         '-e', 'STAY_ALIVE_ON_FAILURE=false',
                         '--net', 'monasca-docker_default',
                         '--name', 'monasca-docker-tempest',
                         'chaconpiza/tempest-tests:test']

    p = subprocess.Popen(tempest_tests_run, stdout=subprocess.PIPE, universal_newlines=True)

    def kill(signal, frame):
        p.kill()
        LOG.warn('Finished by Ctrl-c!')
        sys.exit(2)

    signal.signal(signal.SIGINT, kill)

    start_time = datetime.datetime.now()
    while True:
        output = p.stdout.readline()
        LOG.info(output.strip())
        return_code = p.poll()
        if return_code is not None:
            LOG.debug('RETURN CODE: {0}'.format(return_code))
            if return_code != 0:
                LOG.error('Tempest-tests failed !!!')
                raise TempestTestFailedException()
            if return_code == 0:
                LOG.info('Tempest-tests succeeded')
            # Process has finished, read rest of the output 
            #for output in p.stdout.readlines():
            #    LOG.error(output.strip())
            break
        end_time = start_time + datetime.timedelta(minutes=20)
        if datetime.datetime.now() >= end_time:
            LOG.error('Tempest-tests timed out at 20 min !!!')
            p.kill()
            raise TempestTestFailedException()


def handle_other(files, modules, tags, pipeline):
    LOG.warn('Unsupported event type "%s", nothing to do.' % (
        os.environ.get('TRAVIS_EVENT_TYPE')))


def print_env(pipeline, voting, to_print=True):
    environ_vars = {'environment_details': {
        'TRAVIS_COMMIT': os.environ.get('TRAVIS_COMMIT'),
        'TRAVIS_COMMIT_RANGE': os.environ.get('TRAVIS_COMMIT_RANGE'),
        'TRAVIS_PULL_REQUEST': os.environ.get('TRAVIS_PULL_REQUEST'),
        'TRAVIS_PULL_REQUEST_SHA': os.environ.get('TRAVIS_PULL_REQUEST_SHA'),
        'TRAVIS_PULL_REQUEST_SLUG': os.environ.get('TRAVIS_PULL_REQUEST_SLUG'),
        'TRAVIS_SECURE_ENV_VARS':  os.environ.get('TRAVIS_SECURE_ENV_VARS'),
        'TRAVIS_EVENT_TYPE': os.environ.get('TRAVIS_EVENT_TYPE'),
        'TRAVIS_BRANCH': os.environ.get('TRAVIS_BRANCH'),
        'TRAVIS_PULL_REQUEST_BRANCH': os.environ.get('TRAVIS_PULL_REQUEST_BRANCH'),
        'TRAVIS_TAG': os.environ.get('TRAVIS_TAG'),
        'TRAVIS_COMMIT_MESSAGE': os.environ.get('TRAVIS_COMMIT_MESSAGE'),
        'TRAVIS_BUILD_ID': os.environ.get('TRAVIS_BUILD_ID'),
        'TRAVIS_JOB_NUMBER': os.environ.get('TRAVIS_JOB_NUMBER'),
        'CI_PIPELINE': pipeline,
        'CI_VOTING': voting }}

    if to_print:
        LOG.info(json.dumps(environ_vars, indent=2))
    return environ_vars


def main():
    pipeline = args.pipeline
    voting = not args.non_voting
    printlogs = args.print_logs

#    if os.environ.get('TRAVIS_BRANCH', None) != 'master':
#        LOG.warn('Not master branch, skipping tests.')
#        exit(2)
    if not pipeline or pipeline not in ('logs', 'metrics'):
        LOG.warn('Unkown pipeline: {0}'.format(pipeline))
        exit(2)

    print_env(pipeline, voting)
    set_log_dir()

    files = get_changed_files()
    LOG.info('get_changed_files():'.format(files))
    modules = get_dirty_modules(files)
    LOG.info('get_dirty_modules():'.format(modules))
    tags = get_message_tags()
    LOG.info('get_message_tags():'.format(tags))

    if tags:
        LOG.debug('Tags detected:')
        for tag in tags:
            LOG.debug('  '.format(tag))
    else:
        LOG.info('No tags detected.')

    func = {
        'pull_request': handle_pull_request,
        'push': handle_push
    }.get(os.environ.get('TRAVIS_EVENT_TYPE', None), handle_other)

    try:
        func(files, modules, tags, pipeline)
    except (FileReadException, FileWriteException):
        # those error must terminate the CI
        raise
    except (InitJobFailedException, SmokeTestFailedException,
            TempestTestFailedException):
        if voting:
            exit(1)
        else:
            LOG.warn('{0} is not voting, skipping failure'.format(pipeline))
    finally:
        output_docker_ps()
        output_docker_logs()
        uploaded_files = upload_log_files(printlogs)
#        upload_manifest(pipeline, voting, uploaded_files, modules, files, tags)


if __name__ == '__main__':
    main()
