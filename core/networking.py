# Classes for network communication.
# Following http://zguide.zeromq.org/py:all#toc46

import json
import logging
import threading
import zmq
import time
import sys
from zmq.eventloop.ioloop import IOLoop, PeriodicCallback
from zmq.eventloop.zmqstream import ZMQStream
from warnings import warn
from collections import deque
from itertools import count

# Message structure:
# Messages flow from the Terminal class to the raspberry pi
# {key: {target, value}}
# key - what type of message is this (byte string)
# target - where should it go (json encoded on receipt, converted to byte string)
#          For the Pilot, target refers to the mouse so the Terminal can plot/store data
# value - what is the message (json encoded)
# This structure allows us to sensibly 'unwrap' messages:
# the handler function for each socket passes the target and value to the appropriate function for a given key
# the key function will do whatever it needs to do with value and send it on to target

# Listens

# TODO: Periodically ping pis to check that they are still responsive

class Terminal_Networking:
    # Internal Variables/Objects to the Terminal Networking Object
    ctx          = None    # Context
    loop         = None    # IOLoop
    pub_port     = None    # Publisher Port
    listen_port  = None    # Listener Port
    message_port = None    # Messenger Port
    publisher    = None    # Publisher Handler - For sending messages to the pis
    listener     = None    # Listener Handler - For receiving data from the pis
    messenger    = None    # Messenger Handler - For receiving messages from the Terminal Class


    def __init__(self, prefs=None, start=False):
        # Prefs should be passed by the terminal, if not, try to load from default location
        if not prefs:
            try:
                with open('/usr/rpilot/prefs.json') as op:
                    prefs = json.load(op)
            except:
                logging.exception('No Prefs passed to networking class!')

        self.pub_port = prefs['PUBPORT']
        self.listen_port = prefs['LISTENPORT']
        self.message_port = prefs['MESSAGEPORT']

        self.context = zmq.Context.instance()
        self.loop = IOLoop.instance()
        # Publisher sends commands to Pilots
        # Subscriber listens for data from Pilots
        # Sync ensures pilots are ready for publishing
        self.publisher  = self.context.socket(zmq.PUB)
        self.listener   = self.context.socket(zmq.PULL)
        self.messenger  = self.context.socket(zmq.PULL)
        self.publisher.bind('tcp://*:{}'.format(self.pub_port))
        self.listener.bind('tcp://*:{}'.format(self.listen_port))
        self.messenger.connect('tcp://localhost:{}'.format(self.message_port))

        # Wrap as ZMQStreams
        #self.publisher = ZMQStream(self.publisher)
        self.listener  = ZMQStream(self.listener)
        self.messenger = ZMQStream(self.messenger)

        # Set on_recv action to handle data and messages
        self.listener.on_recv(self.handle_listen)
        self.messenger.on_recv(self.handle_message)

        # Message dictionary - What method to call for each type of message received by the terminal class
        self.messages = {
            'PING': self.m_ping,    # We are asked to confirm that the raspberry pis are alive
            'INIT': self.m_init,    # We should ask all the pilots to confirm that they are alive
            'START': self.m_start_task,  # Upload task to a pilot
            'CHANGE': self.m_change,  # Change a parameter on the Pi
            'STOP': self.m_stop,
            'STOPALL': self.m_stopall,
            'RECVD': self.m_recvd # We are getting confirmation that the message was received
        }

        # Listen dictionary - What to do with pushes from the raspberry pis
        self.listens = {
            'DATA': self.l_data, # Stash incoming data from an rpilot
            'ALIVE': self.l_alive, # A Pi is responding to our periodic query of whether it remains alive
                                   # It replies with its subscription filter
            'EVENT': self.l_event, # Stash a single event (not a whole trial's data)
            'STATE': self.l_state, # The Pi is confirming/notifying us that it has changed state
            'RECVD': self.m_recvd  # We are getting confirmation that the message was received
        }

        self.mice_data = {} # maps the running mice to methods to stash their data
        self.subscribers = set()
        # TODO: Subscribers should actually be a dict with names and # of expected subscribers
        self.states = {} # States of each of the pilots

        self.msg_counter = count()
        self.outbox  = {} # Messages that are out with unconfirmed delivery
        self.timers = {} # dict of timer threads that will check in on outbox messages

        # TODO: Get KVMsg from zexamples repo

        if start:
            self.start()

    def start(self):
        try:
            self.thread = threading.Thread(target=self.loop.start)
            self.thread.start()
        except KeyboardInterrupt:
            pass

    def stop(self):
        try:
            pass
        # TODO: Call this when shutting down
        except:
            pass

    def publish(self, target, message):
        # target is the subscriber filter
        # Message is a dict that should have two k/v pairs:
        # 'key': the type of message this is
        # 'value': the content of the message

        # Check if we know about this target
        # Warn if not, but still try and send message

        # TODO: Make more robust - number each publish, spawn timer thread that checks whether we have received receipt
        # TODO: Bind an on_send callback that spawns the timer thread
        # TODO: Then add a 'TTL' field in the message dict and check for it in this function
        # TODO: Add 'TTL' to prefs setup
        # TODO: Add timer value to prefs setup

        # Give the message a number and TTL, stash it
        msg_num = str(self.msg_counter.next())
        message['id'] = msg_num
        message['target'] = target
        self.outbox[msg_num] = message

        if target not in self.subscribers:
            logging.warning('PUBLISH {} - Message to unconfirmed target: {}'.format(msg_num, target))

        # Make sure our message has everything
        if not all(i in message.keys() for i in ['key', 'value']):
            logging.warning('PUBLISH {} - Improperly formatted: {}'.format(msg_num, message))
            return

        logging.info('PUBLISH {} - TARGET: {}, MESSAGE: {}'.format(msg_num, target, message))

        # Publish the message
        self.publisher.send_multipart([bytes(target), json.dumps(message)])

        # Spawn a thread to check in on our message
        self.timers[msg_num] = threading.Timer(10.0, self.p_repeat, args=(msg_num,))
        self.timers[msg_num].start()



    def handle_listen(self, msg):
        # listens are always json encoded, single-part messages
        msg = json.loads(msg[0])
        if isinstance(msg, unicode or basestring):
            msg = json.loads(msg)

        # Check if our listen was sent properly
        if not all(i in msg.keys() for i in ['key','value','target']):
            logging.warning('LISTEN Improperly formatted: {}'.format(msg))
            return

        # Log and spawn thread to respond to listen
        logging.info('LISTEN - KEY: {}, VALUE: {}'.format(msg['key'],msg['value']))
        listen_funk = self.listens[msg['key']]
        listen_thread = threading.Thread(target=listen_funk, args=[msg['target'],msg['value']])
        listen_thread.start()


    def handle_message(self, msg):
        # messages are always json encoded, single-part messages.
        msg_enc = json.loads(msg[0])
        if isinstance(msg_enc, unicode or basestring):
            msg_enc = json.loads(msg_enc)

        print msg_enc
        sys.stdout.flush()

        # Check if message was formatted properly
        if not all(i in msg_enc.keys() for i in ['key', 'target', 'value']):
            logging.warning('MESSAGE Improperly formatted: {}'.format(msg))

        # Log and spawn thread to respond to message
        logging.info('MESSAGE - KEY: {}, TARGET: {}, VALUE: {}'.format(msg_enc['key'], msg_enc['target'], msg_enc['value']))
        message_funk = self.messages[msg_enc['key']]
        message_thread = threading.Thread(target=message_funk, args=[msg_enc['target'], msg_enc['value']])
        message_thread.start()



    ##########################
    # Message Handling Methods

    def m_ping(self, target, value):
        # Ping a specific subscriber and ask if they are alive
        self.publish(target, {'key':'PING', 'value':''})

    def m_init(self, target, value):
        # Ping all pis that we are expecting given our pilot db until we get a response
        # Pass back who responds as they do to update the GUI.

        # Override target if any is fed to us
        target = b'X' # TODO: Allow all pub/sub key to be defined in setup functions

        # Publish a general ping five times, m_alive will update our list of subscribers as they respond
        for i in range(5):
            self.publisher.send_multipart([bytes(target), json.dumps({'key':'PING', 'value':''})])
            time.sleep(1)

        # If we still haven't heard from pis that we expected to, we'll ping them a few more times
        pis = set(value)

        if not len(pis - self.subscribers) == 0:
            for i in range(5):
                awol_pis = pis - self.subscribers
                for p in awol_pis:
                    self.publisher.send_multipart([bytes(p), json.dumps({'key': 'PING', 'value': ''})])
                time.sleep(2)

            # If we still haven't heard from them, tell the terminal that
            awol_pis = pis - self.subscribers
            for p in awol_pis:
                logging.warning('Requested Pilot {} was not heard from'.format(p))
                self.publish('T',{'key':'DEAD','value':p})



    def m_start_task(self, target, value):
        pass


        # Start listening thread

    def m_change(self, target, value):
        # TODO: Should also handle param changes to GUI objects like ntrials, etc.
        pass


    def m_stop(self, target, value):
        # also pop mouse value from data function dict
        pass

    def m_stopall(self, target, value):
        pass

    def m_recvd(self, target, value):
        # confirmation that a published message was received
        # value should be the message id

        # delete message from outbox if we still have it
        if value in self.outbox.keys():
            del self.outbox[value]

        # stop a timer thread if we have it
        if value in self.timers.keys():
            self.timers[value].cancel()

        logging.info('CONFIRMED MESSAGE {}'.format(value))

    def l_data(self, target, value):
        pass

    def l_alive(self, target, value):
        # A pi has told us that it is alive and what its filter is
        self.subscribers.update(value)
        logging.info('Received ALIVE from {}'.format(value))
        # Tell the terminal
        self.publish('T',{'key':'ALIVE','value':value})

    def l_event(self, target, value):
        pass

    def l_state(self, target, value):
        pass

    def p_repeat(self, message_id):
        # Handle repeated messages
        # If we still have the message in our outbox...
        if message_id not in self.outbox.keys():
            logging.warning('Republish called for message {}, but missing message'.format(message_id))
            return

        # if it doesn't have a TTL, set it, if it does, decrement it
        if 'ttl' in self.outbox[message_id].keys():
            self.outbox[message_id]['ttl'] = int(self.outbox[message_id]['ttl'])-1
        else:
            self.outbox[message_id]['ttl'] = 5 # TODO: Get this value from prefs

        # Otherwise send the message and spawn another timer thread
        logging.info('REPUBLISH {} - TARGET: {}, MESSAGE: {}'.format(message_id,
                                                                     self.outbox[message_id]['target'],
                                                                     self.outbox[message_id]))

        # If our TTL is now zero, delete the message and log its failure
        if int(self.outbox[message_id]['ttl']) <= 0:
            logging.warning('PUBLISH FAILED {} - {}'.format(message_id, self.outbox[message_id]))
            del self.outbox[message_id]
            return

        # Publish the message
        self.publisher.send_multipart([bytes(self.outbox[message_id]['target']), json.dumps(self.outbox[message_id])])

        # Spawn a thread to check in on our message
        self.timers[message_id] = threading.Timer(10.0, self.p_repeat, args=(message_id,))
        self.timers[message_id].start()




class Pilot_Networking:
    # Internal Variables/Objects to the Pilot Networking Object
    ctx          = None    # Context
    loop         = None    # IOLoop
    sub_port     = None    # Subscriber Port to Terminal Publisher
    push_port    = None    # Port to push messages back to Terminal
    message_port = None    # Port to receive messages from the Pilot
    subscriber   = None    # Subscriber Handler - For receiving messages from the terminal
    pusher       = None    # Pusher Handler - For pushing data back to the terminal
    messenger    = None    # Messenger Handler - For receiving messages from the Pilot

    def __init__(self, prefs, start=False):
        self.name = prefs['NAME']

        self.sub_port = prefs['SUBPORT']
        self.push_port = prefs['PUSHPORT']
        self.message_port = prefs['MSGPORT']

        self.context = zmq.Context.instance()

        self.loop = IOLoop.instance()

        # Instantiate and connect sockets
        self.subscriber = self.context.socket(zmq.SUB)
        self.pusher     = self.context.socket(zmq.PUSH)
        self.messenger  = self.context.socket(zmq.PULL)

        self.subscriber.connect('tcp://{}:{}'.format(prefs['TERMINALIP'],self.sub_port))
        self.pusher.connect('tcp://{}:{}'.format(prefs['TERMINALIP'],self.push_port))
        self.messenger.connect('ipc://{}.ipc'.format(self.message_port))

        # Set subscriber filters
        self.subscriber.setsockopt(zmq.SUBSCRIBE, bytes(prefs['NAME']))
        self.subscriber.setsockopt(zmq.SUBSCRIBE, b'X')

        # Wrap as ZMQStreams
        self.subscriber = ZMQStream(self.subscriber)
        self.messenger  = ZMQStream(self.messenger)

        # Set on_recv callbacks
        self.subscriber.on_recv(self.handle_listen)
        self.messenger.on_recv(self.handle_message)

        # Message dictionary - What method to call for messages from the parent Pilot
        self.messages = {
            'STATE': self.m_state, # Confirm or notify terminal of state change
            'DATA': self.m_data,  # Sending a whole trial's data back
            'EVENT': self.m_event, # Sending a single event within a trial back
            'COHERE': self.m_cohere # Sending our temporary data table at the end of a run to compare w/ terminal's copy
        }

        # Listen dictionary - What method to call for PUBlishes from the Terminal
        self.listens = {
            'PING': self.l_ping, # The Terminal wants to know if we're listening
            'START': self.l_start, # We are being sent a task to start
            'STOP': self.l_stop, # We are being told to stop the current task
            'CHANGE': self.l_change # The Terminal is changing some task parameter
        }

        if start:
            self.start()

    def start(self):
        try:
            self.thread = threading.Thread(target=self.loop.start)
            self.thread.start()
        except KeyboardInterrupt:
            pass

    def push(self, key, target='', value=''):
        self.pusher.send_json(json.dumps({'key':key, 'target':target, 'value':value}))

    def handle_listen(self, msg):
        # listens are always json encoded, single-part messages
        msg = json.loads(msg[1])

        # Check if our listen was sent properly
        if not all(i in msg.keys() for i in ['key','value']):
            logging.warning('LISTEN Improperly formatted: {}'.format(msg))
            return

        # Log and spawn thread to respond to listen
        logging.info('LISTEN - KEY: {}, VALUE: {}'.format(msg['key'],msg['value']))
        listen_funk = self.listens[msg['key']]
        listen_thread = threading.Thread(target=listen_funk, args=(msg['value'],))
        listen_thread.start()

        # Then let the terminal know we got the message
        recvd_thread = threading.Thread(target=self.push, kwargs={'key':'RECVD','value':msg['id']})
        recvd_thread.start()

    def handle_message(self):
        pass

    ###########################3
    # Message/Listen handling methods
    def m_state(self, target, value):
        pass

    def m_data(self, target, value):
        pass

    def m_event(self, target, value):
        pass

    def m_cohere(self, target, value):
        # Send our local version of the data table so the terminal can double check
        pass

    def l_ping(self, value):
        # The terminal wants to know if we are alive, respond with our name
        self.push('ALIVE', value=self.name)

    def l_start(self, value):
        pass

    def l_stop(self, value):
        pass

    def l_change(self, value):
        pass






































