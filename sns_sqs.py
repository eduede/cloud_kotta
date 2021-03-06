#!/usr/bin/env python
import boto
import sys
import json
import time
import boto.sqs
import config_manager as cm
import uuid
import ast

def get_uuid():
    return str(uuid.uuid1())

def publish(sns_conn, topicARN, message):
    sns_conn.publish(topic=topicARN, message=message)

def sns_test(sns_conn, topic_arn):
    uid = get_uuid()
    data = {"job_id"           : uid,
            "username"         : "yadu",
            "jobtype"          : "doc2vec",
            "inputs"           : [{"src": "https://s3.amazonaws.com/klab-jobs/inputs/test.txt", "dest": "test.txt" }],
            "outputs"          : [{"src": "doc_mat.pkl",  "dest": "klab-jobs/outputs/{0}/doc_mat.pkl".format(uid)},
                                  {"src": "word_mat.pkl", "dest": "klab-jobs/outputs/{0}/word_mat.pkl".format(uid)},
                                  {"src": "mdl.pkl",      "dest": "klab-jobs/outputs/{0}/mdl.pkl".format(uid)}],
            "submit_time"      : int(time.time()),
            "status"           : "pending"
    }
    print "Publishing Dummy task to SNS topic"
    publish(sns_conn, topic_arn, json.dumps(data))
    return data

def sqs_test(sqs_conn, queue_name):

    q   = sqs_conn.get_queue(queue_name)
    msg = q.get_messages(1)
    print len(msg)

    r = json.loads(msg[0].get_body())["Message"]
    print r
    print q.delete_message(msg[0])
    #req =  json.loads(msg[0].get_body()["Message"])


# Send a message to the queue with message_attributes
def send_sqs_msg(conn, q, message, message_attr):
    print "Sending message"
    status = conn.send_message(q, message, message_attributes=message_attr)
    return status

# Get the right message from the queue
def get_msg_with_attr(conn, q,  message_attr):
    while (1):
        # Introduce a higher visibility timeout to avoid the refresher seeing the same
        # items repeatedly.
        msg = q.read(visibility_timeout=10, wait_time_seconds=1, message_attributes=['All']) # visibility_timeout=10,
        # Case 1 We have a valid message
        if msg:
            if "job_id" in msg.message_attributes and "type" in msg.message_attributes:
                # Case:2 Valid message, and the right message we are looking for
                if msg.message_attributes["job_id"]["string_value"] == message_attr["job_id"]["string_value"]:
                    if msg.message_attributes["type"]["string_value"] == message_attr["type"]["string_value"]:
                        print "We have a matching refresh job request"
                        print msg.get_body()
                        return msg
                    else:
                        continue
                # Case:3 Valid message but not the one we are looking for
                else:
                    # If we don't do anything here, the message itself will timeout and get picked up
                    # by the right worker.
                    continue
        # Case:4 We have exhausted all the messages in the queue, and a message with the attributes we are
        # looking for are not found.
        else:
            print "[ERROR] Could not get the message for refresh cycle"
            return None

    return None

def send_test_message(sqs_conn, q):
    job_id = get_uuid()
    attr = {"job_id": {"data_type"   : "String",
                       "string_value": job_id},
            "type"  : {"data_type"   : "String",
                       "string_value": "refresh"}}
    msg  = {"job_id"   : job_id,
            "walltime" : 400,
            "queue"    : "Test"}

    r = send_sqs_msg(sqs_conn, q, json.dumps(msg), attr)
    return r

def refresh_message(app, msg):

    sqs_conn = app.config["sqs.conn"]
    sqs_name = app.config["instance.tags"]["JobsQueueName"]

    q = sqs_conn.get_queue(sqs_name)

    print "Refreshing message"
    print msg.get_body()
    msg_body = msg.get_body()
    m        = json.loads(msg_body)["Message"]

    data     = ast.literal_eval(m)
    job_id   = data.get('job_id')

    print "Refreshing Job_id", job_id

    attr = {"job_id": {"data_type"   : "String",
                       "string_value": job_id},
            "type"  : {"data_type"   : "String",
                       "string_value": "refresh"}}

    # Launch the refresh task
    status  = send_sqs_msg(sqs_conn, q, msg_body, attr)
    print "Send refresh message status : ", status
    new_msg = get_msg_with_attr(sqs_conn, q, attr)
    if new_msg :
        print "Found our message"
        print "deleting old message"
        q.delete_message(msg)

    else:
        print "No messages. Failed"

    print "New_msg : ", new_msg
    #sqs_conn.change_message_visibility(q, new_msg, 60*1)
    sqs_conn.change_message_visibility_batch(q, [(new_msg, app.config["sqs.message_visibility_timeout"])] )
    #sqs_conn.change_message_visibility(q, status, 60*1)
    return new_msg

def post_message_to_pending(app, pending_q, msg, job_id):
    print "Posting job_id:{0} to pending_q:{1} ".format(job_id, pending_q)
    # Take the message and put it in the pending_q
    current_msg = app.config['sqs.conn'].send_message(pending_q, msg)
    return current_msg


def post_message_to_active(app, active_q, msg, job_id):
    print "job_id : ", job_id
    print "instance_id : ", app.config["instance_id"]

    attr = {"instance_id": {"data_type"   : "String",
                            "string_value": app.config["instance_id"]},
            "job_id"     : {"data_type"   : "String",
                            "string_value": job_id } }
    # Take the message and put it in the active_q
    current_msg = app.config['sqs.conn'].send_message(active_q, msg, message_attributes=attr)
    return attr, current_msg

def delete_message_from_active(sqs_conn, active_q, attr):
    
    while (1):
        messages = active_q.get_messages(num_messages=10, visibility_timeout=2, wait_time_seconds=2, message_attributes=['All'])
        if not messages:
            return None
        for msg in messages:
            if msg.message_attributes["instance_id"]["string_value"] == attr["instance_id"]["string_value"] and msg.message_attributes["job_id"]["string_value"] == attr["job_id"]["string_value"]:
                active_q.delete_message(msg)
                return 1

    return None                         

if __name__ == "__main__":
    app = cm.load_configs("production.conf")
    
    sqs_conn  = app.config["sqs.conn"]
    pending   = app.config["instance.tags"]["JobsQueueName"]
    active    = app.config["instance.tags"]["ActiveQueueName"]
    pending_q = sqs_conn.get_queue(pending)
    active_q  = sqs_conn.get_queue(active)

    msg = {"job_id" : "fooo",
           "walltime" : "aaaa"}
    attr, msg = post_message_to_active(app, active_q, json.dumps(msg), get_uuid())
    print "Posted mesage : ", msg, attr

    time.sleep(10)
    st = delete_message_from_active(sqs_conn, active_q, attr)

    print "Delete : ", st
    exit(0)

    #r   = send_test_message(sqs_conn, q)
    while (1):
        msg = q.read(wait_time_seconds=10)
        if  msg:
            print "Received a message"
            time.sleep(4)
            new_msg = refresh_message(app, msg)
            q.delete_message(msg)
            print "Refreshed and deleted old message"
        else:
            "Sleeping ..."


#sns_test(app.config["sns.conn"], app.config["instance.tags"]["JobsSNSTopicARN"])
#sqs_test(app.config["sqs.conn"], app.config["instance.tags"]["JobsQueueName"])
