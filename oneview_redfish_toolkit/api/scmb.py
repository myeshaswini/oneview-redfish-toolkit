# -*- coding: utf-8 -*-

# Copyright (2017-2018) Hewlett Packard Enterprise Development LP
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

import json
import logging
import os
import pika
import ssl

from copy import deepcopy
from hpOneView.exceptions import HPOneViewException
from hpOneView.oneview_client import OneViewClient
from pika.credentials import ExternalCredentials

from oneview_redfish_toolkit.api.event import Event
from oneview_redfish_toolkit import util


ONEVIEW_CA = "oneview_redfish_toolkit/certs/oneview_ca.pem"
SCMB_CERT = "oneview_redfish_toolkit/certs/oneview_scmb.pem"
SCMB_KEY = "oneview_redfish_toolkit/certs/oneview_scmb.key"
SCMB_PORT = 5671
SCMB_SOCKET_TIMEOUT = 5  # seconds
SCMB_RESOURCE_LIST = [
    'alerts',
    'enclosures',
    'racks',
    'server-hardware']
SCMB_EXCHANGE_NAME = 'scmb'


def check_cert_exist():
    return os.path.isfile(ONEVIEW_CA) & os.path.isfile(SCMB_CERT) & \
        os.path.isfile(SCMB_KEY)


def get_oneview_client():
    # Workaround for #328
    # Create OneView client using API 500 just to retrieve OneView certificates
    try:
        ov_config = deepcopy(util.ov_config)
        ov_config['api_version'] = 500
        ov_client = OneViewClient(ov_config)
        ov_client.connection.login(ov_config['credentials'])
    # if failed abort
    except Exception:
        raise

    return ov_client


def get_cert():
    # Get CA
    ov_client = get_oneview_client()

    cert = ov_client.certificate_authority.get()

    with open(ONEVIEW_CA, 'w+') as f:
        f.write(cert)
    # Generate scmb Cert:
    try:
        cert_info = {
            "commonName": "default",
            "type": "RabbitMqClientCertV2"
        }
        ov_client.certificate_rabbitmq.generate(cert_info)
    except HPOneViewException as e:
        # Cert with that commonName already exists. We are going to get it
        if e.oneview_response["errorCode"] == 'RABBITMQ_CLIENTCERT_CONFLICT':
            logging.info('Certs already exists in oneview')
        else:
            # Another error is not expected, we raise.
            logging.exception("Unexpected error")
            raise
    # Get the scmb certs key pair
    certs = ov_client.certificate_rabbitmq.get_key_pair(
        'default')
    # Save cert
    with open(SCMB_CERT, 'w+') as f:
        f.write(certs['base64SSLCertData'])
    # Save key
    with open(SCMB_KEY, 'w+') as f:
        f.write(certs['base64SSLKeyData'])


def scmb_connect():
    scmb_server = util.config['oneview_config']['ip']

    # Setup our ssl options
    ssl_options = ({'ca_certs': ONEVIEW_CA,
                    'certfile': SCMB_CERT,
                    'keyfile': SCMB_KEY,
                    'cert_reqs': ssl.CERT_REQUIRED,
                    'server_side': False})

    scmb_connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            scmb_server,
            SCMB_PORT,
            credentials=ExternalCredentials(),
            socket_timeout=SCMB_SOCKET_TIMEOUT,
            ssl=True,
            ssl_options=ssl_options))

    return scmb_connection


def is_cert_working_with_scmb():
    # Create and bind to queue
    EXCHANGE_NAME = 'scmb'
    ROUTE = 'scmb.alerts.#'
    try:
        scmb_conn = scmb_connect()
        channel = scmb_conn.channel()
        queue = channel.queue_declare(auto_delete=True)
        channel.queue_bind(
            queue=queue.method.queue,
            exchange=EXCHANGE_NAME,
            routing_key=ROUTE)
        channel.close()
        scmb_conn.close()
    except Exception:
        logging.exception("Failed to test scmb connection")
        return False
    return True


def consume_message(ch, method, properties, body):
    body = json.loads(body.decode('utf-8'))
    resource = body['resource']

    if (resource['category'] == 'alerts'):
        category = resource['associatedResource']['resourceCategory']
    else:
        category = resource['category']

    if (category in SCMB_RESOURCE_LIST):
        event = Event(body)

        util.dispatch_event(event)
    else:
        logging.debug('SCMB message received for an unmanaged resource')


def listen_scmb():
    try:
        scmb_conn = scmb_connect()
        ch = scmb_conn.channel()

        queue_name = ch.queue_declare(auto_delete=True)

        for resource in SCMB_RESOURCE_LIST:
            # scmb.<resource>.#
            route = SCMB_EXCHANGE_NAME + '.' + resource + '.#'

            ch.queue_bind(
                queue=queue_name.method.queue,
                exchange=SCMB_EXCHANGE_NAME,
                routing_key=route)

        ch.basic_consume(consume_message, queue=queue_name.method.queue)
        ch.start_consuming()
    except KeyboardInterrupt:
        ch.close()
        scmb_conn.close()
    except Exception:
        logging.exception("Failed to listen to scmb messages")
