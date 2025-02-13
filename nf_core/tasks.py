import csv
import json
import os
import random
import time
import sys
from datetime import datetime

import requests
from xml_to_dict import XMLtoDict
from urllib.parse import unquote_plus, quote_plus

from celery import shared_task

from nf_core.helper import formatMobileNumber, writeErrorLog, writeActivityLog
from nf_core.models import Contacts, SMSQueueHandler, SMSHistory, User, SMSSchedule

from nf_api.views import sendSMSCore

from Notifier.celery import app

from django.conf import settings
from django.core.management import call_command

try:
    from . import scheduled_task
except Exception:
    pass


@shared_task
def uploadContact(file_path, user_id, group_id):
    """
    Manage the contact upload task queue
    """
    try:
        contacts_to_add = []
        with open(file_path, encoding='utf-8') as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=',')
            success_count = 0
            failed_count = 0
            for row in csv_reader:
                name = None
                try:
                    name = row[1]
                    if name == "" or name is None:
                        name = None
                except Exception as e:
                    pass
                if row[0] != "" and row[0] is not None:
                    success, mobile = formatMobileNumber(row[0])
                    if success:
                        try:
                            contacts_to_add.append(Contacts(
                                user_id=user_id,
                                group_id=group_id,
                                name=name,
                                mobile=mobile
                            ))
                        except Exception as e:
                            pass
                            failed_count += 1
                        success_count += 1
                    else:
                        failed_count += 1
                else:
                    failed_count += 1

        Contacts.objects.bulk_create(contacts_to_add)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        return str(e)


def getDeliveryStatus(aggregator_operator_config, receiver, shoot_id):
    # AGGREGATOR OPERATOR CONFIG
    url = aggregator_operator_config.get('delivery_status_url')
    username = aggregator_operator_config.get('username')
    password = aggregator_operator_config.get('password')
    apiKey = aggregator_operator_config.get('api_key')
    billMsisdn = aggregator_operator_config.get('bill_msisdn')

    payload = {
        "username": username,
        "password": password,
        "apiKey": apiKey,
        "billMsisdn": billMsisdn,
        "msisdnList": [receiver],
        "serverReference": shoot_id
    }
    header = {"Content-Type": "application/json"}
    response = requests.post(url, data=json.dumps(payload), headers=header)
    response = response.json()
    return response


@shared_task(bind=True, default_retry_delay=30, max_retries=3)
def updateSMSStatus(self, *args, **kwargs):
    try:
        # AGGREGATOR OPERATOR CONFIG
        aggregator_operator_config = kwargs.get('aggregator_operator_config')

        json_payload = kwargs
        sms_instance = SMSHistory.objects.get(id=json_payload['sms_id'], user_id=json_payload['user_id'])
        sms_instance.shoot_id = json_payload['shoot_id']
        sms_instance.receiver = json_payload['receiver']
        sms_instance.sender_id = json_payload['sender_id']
        sms_instance.status = json_payload['status']
        sms_instance.failure_reason = json_payload['failure_reason']
        sms_instance.save()

        if sms_instance.shoot_id is not None:
            ########### Check SMS Delivery Status here ###########
            response = getDeliveryStatus(aggregator_operator_config, sms_instance.receiver, sms_instance.shoot_id)
            serverResponseCode = int(response['serverResponseCode'])
            serverResponseMessage = response['serverResponseMessage']

            if response["deliveryStatus"]:
                d_status = response["deliveryStatus"][0].split('-')[1]
                if d_status == "Delivery Pending":
                    d_status = "Processing"
                elif d_status == "UnDelivered":
                    d_status = "Failed"
            else:
                d_status = "Failed"

            sms_instance.status = d_status
            sms_instance.api_response = response
            sms_instance.save()
            ########### End Check SMS Delivery Status here ###########

            server_failure_codes = [9011, 9012, 9015, 9099]

            if serverResponseCode != 9000:
                sms_instance.status = "Failed"
                sms_instance.failure_reason = f"Error Code: {serverResponseCode} | Message: {serverResponseMessage}"
            else:
                check_counter = 0
                # while serverResponseCode in server_failure_codes:
                #     if check_counter >= 5:
                #         break
                while check_counter < 5:
                    response = getDeliveryStatus(sms_instance.sender_id, sms_instance.receiver, sms_instance.shoot_id)
                    serverResponseCode = int(response['serverResponseCode'])
                    serverResponseMessage = response['serverResponseMessage']

                    if response["deliveryStatus"]:
                        d_status = response["deliveryStatus"][0].split('-')[1]
                        if d_status == "Delivery Pending":
                            d_status = "Processing"
                        elif d_status == "UnDelivered":
                            d_status = "Failed"
                    else:
                        d_status = "Failed"

                    check_counter += 1
                    time.sleep(10)
                sms_instance.status = d_status
                sms_instance.api_response = response
                sms_instance.save()
                if serverResponseCode != 9000:
                    sms_instance.status = "Failed"
                    sms_instance.failure_reason = f"Error Code: {serverResponseCode} | Message: {serverResponseMessage}"
                else:
                    sms_instance.status = d_status
                    sms_instance.save()
        SMSQueueHandler.objects.filter(sms_id=json_payload['sms_id'], user_id=json_payload['user_id']).delete()
    except Exception:
        self.retry()


@shared_task(bind=True, default_retry_delay=30, max_retries=5)
def microSMSQueue(self, *args, **kwargs):
    update_queue = f"update{random.randint(1, 3)}"
    try:
        sms_body = kwargs.get('sms_body')
        receiver = kwargs.get('receiver')

        # AGGREGATOR OPERATOR CONFIG
        aggregator_operator_config = kwargs.get('aggregator_operator_config')
        url = aggregator_operator_config.get('send_sms_url')
        username = aggregator_operator_config.get('username')
        password = aggregator_operator_config.get('password')
        apiKey = aggregator_operator_config.get('api_key')
        billMsisdn = aggregator_operator_config.get('bill_msisdn')
        cli = aggregator_operator_config.get('default_cli')
        sender_id = aggregator_operator_config.get('bill_msisdn')

        ########### Start call the operator API here ###########
        payload = {
            "username": username,
            "password": password,
            "apiKey": apiKey,
            "billMsisdn": billMsisdn,
            "cli": cli,
            "msisdnList": [receiver],
            "message": sms_body,
            "transactionType": "T",
            "messageType": 3
        }
        header = {"Content-Type": "application/json; charset=utf-8"}
        response = requests.post(url, data=json.dumps(payload), headers=header)
        response = response.json()
        ########### End call the operator API here ###########

        if int(response['serverResponseCode']) == 9000:
            shoot_id = response['serverTxnId']
            update_payload = {
                "aggregator_operator_config": aggregator_operator_config,
                "sms_id": kwargs.get('sms_id'),
                "user_id": kwargs.get('user_id'),
                "shoot_id": shoot_id,
                "sender_id": sender_id,
                "receiver": receiver,
                "status": "Submitted",
                "failure_reason": None
            }
            app.send_task("nf_core.tasks.updateSMSStatus", queue=update_queue, kwargs=update_payload)
            return f"SUBMITTED | SHOOT ID: {shoot_id} | SMS ID: {kwargs.get('sms_id')} | User ID: {kwargs.get('user_id')}"
        else:
            error_msg = f"Error Code: {response['serverResponseCode']} | Message: {response['serverResponseMessage']}"
            update_payload = {
                "aggregator_operator_config": aggregator_operator_config,
                "sms_id": kwargs.get('sms_id'),
                "user_id": kwargs.get('user_id'),
                "shoot_id": None,
                "sender_id": sender_id,
                "receiver": receiver,
                "status": "Failed",
                "failure_reason": error_msg
            }
            app.send_task("nf_core.tasks.updateSMSStatus", queue=update_queue, kwargs=update_payload)
            return f"FAILED | SMS ID: {kwargs.get('sms_id')} | User ID: {kwargs.get('user_id')} | {error_msg}"
    except Exception as e:
        try:
            update_payload = {
                "aggregator_operator_config": aggregator_operator_config,
                "sms_id": kwargs.get('sms_id'),
                "user_id": kwargs.get('user_id'),
                "shoot_id": None,
                "sender_id": sender_id,
                "receiver": receiver,
                "status": "Failed",
                "failure_reason": str(e)
            }
            app.send_task("nf_core.tasks.updateSMSStatus", queue=update_queue, kwargs=update_payload)
        except Exception:
            pass
        self.retry()


@shared_task(bind=True, default_retry_delay=30, max_retries=5)
def sendSMSQueue(self, *args, **kwargs):
    try:
        micro_general_queue = f"micro{random.randint(1, 6)}"
        micro_priority_queue = f"micro{random.randint(7, 10)}"
        SMSQueueHandler.objects.create(
            sms_id=kwargs.get('sms_id'),
            user_id=kwargs.get('user_id'),
            username=kwargs.get('username'),
            sms_count=kwargs.get('sms_count'),
            queue=kwargs.get('queue_name'),
            operator_logo=f"/static/operator/{kwargs.get('operator_name')}.png"
        )

        if "general" in kwargs.get('queue_name'):
            micro_queue = micro_general_queue
        else:
            micro_queue = micro_priority_queue
        
        app.send_task("nf_core.tasks.microSMSQueue", queue=micro_queue, kwargs=kwargs)
        return f"Queued to Micro Successfully. Micro: {micro_queue} | SMS ID: {kwargs.get('sms_id')}"
    except Exception as e:
        self.retry()


@shared_task
def createPgPartition():
    """
    Create the table partition in a periodic manner
    """
    call_command("pgpartition", "--yes")
    return "PG Partition Created."


@shared_task
def processScheduledSMS():
    """
    Process the scheduled sms
    """
    current_timestamp = datetime.now().astimezone()
    sms_scheduled = SMSSchedule.objects.filter(executed=False, scheduled_time__lte=current_timestamp)
    process_count = 0
    error_list = []
    for sms in sms_scheduled:
        try:
            app.send_task("nf_core.tasks.sendSMSQueue", queue=sms.sms_queue, kwargs=json.loads(sms.params))
            sms.executed = True
            sms.save()
            process_count += 1
        except Exception as e:
            error_list.append(str(e))
    return f"{process_count} Scheduled SMS Processed. Errors: {', '.join(error_list)}"
