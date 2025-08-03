# HoppieBridge
python script for XPPython3 in X-Plane 12, to create drefs for Hoppie's ACARS communication.

The idea is to create a bridge for all the developers that would like to use the ACARS service without creating the server communication interface.

This script creates drefs needed to communicate with Hoppie's ACARS system:
- hoppiebridge/send_queue: data, to send messages to Hoppie's ACARS
- hoppiebridge/poll_queue: data, to poll messages from Hoppie's ACARS
- hoppiebridge/callsign: data, to set your callsign

received messages will be in poll_queue, and sent messages should be added to send_queue.

## Message format:
The messages for Hoppie's ACARS should be in JSON format, with the following structure:

{
- "logon": string, your logon string
- "from": string, your callsign
- "to": string, destination callsign or "all"
- "type": string, type of message, one of "progress", "cpdlc", "telex", "ping", "inforeq", "posreq", "position", "datareq", "poll", or "peek".
- "packet": string,  the actual message to send
}

further information can be found at https://www.hoppie.nl/acars/system/tech.html

As Dref do not permit Array of data, inbox and outbox dref will be json like strings that will be encoded and decoded
before sending to the communication bridge.

Strings sent to outbox dref will be like:

{"to": "SERVER", "type": "inforeq", "packet": "METAR LIPE"}

Received messages, alike, will be json like string:

{'response': 'ok {acars info {LIPE 031350Z 05009KT 010V090 9999 BKN055 28/13 Q1014}}'}

Following Hoppie's ACARS suggestions, poll will be activated every 65 seconds, while outbox will be checked every 5 seconds.

When a message requiring an answer is sent, poll frequency will change to 20 seconds until an answer is received

Copyright (c) 2025, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
