# HoppieBridge
python script for XPPython3 in X-Plane 12, to create drefs for Hoppie's ACARS communication.

The idea is to create a bridge for all the developers that would like to use the ACARS service without creating the server communication interface.

## Requirements
- MacOS 10.14, Windows 7 and Linux kernel 4.0 and above
- X-Plane 12.3.0 and above (not tested with previous versions, may work)
- pbuckner's [XPPython3 plugin](https://xppython3.readthedocs.io/en/latest/index.html)

> [!IMPORTANT]
> **This script needs XPPython version 4.6.0 or newer**


This script creates drefs needed to communicate with Hoppie's ACARS system:
- hoppiebridge/send_queue: string, to send messages to Hoppie's ACARS (legacy)
- hoppiebridge/send_message_to — string, destination callsign for structured message.
- hoppiebridge/send_message_type — mstring, message type for structured message.
hoppiebridge/send_message_packet — string, message packet for structured message.
- hoppiebridge/callsign: string, callsign value
- hoppiebridge/send_callsign — string, set / change callsign.
- hoppiebridge/poll_queue: string, to poll messages from Hoppie's ACARS (legacy)
- hoppiebridge/poll_message_origin — string, origin of the latest message received ("poll" or "response").
- hoppiebridge/poll_message_from — string, source callsign of the latest message received.
- hoppiebridge/poll_message_type — string, type of the latest message received.
- hoppiebridge/poll_message_packet — string, packet content of the latest message received.
- hoppiebridge/poll_queue_clear — number, set to 1 (or any non-zero value) to clear the inbox datarefs when message is received from client.
- hoppiebridge/comm_ready — number, set to 1 (or any non-zero value) to notify unit has all conditions to work:
    - avionics on
    - callsign set
    - poll success.

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

Copyright (c) 2026, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
