import sys, os, requests
import tempfile
import json

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(parent_dir)

# app.py
from flask import Flask, render_template, request, jsonify
import qna.question_service as qs
import qna.publish_to_pubsub_embed as pbembed
import qna.pubsub_chunk_to_store as pb
import qna.pubsub_manager as pubsub
import logging
import bot_help

app = Flask(__name__)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/reindex', methods=['GET'])
def reindex():
    return render_template('reindex.html')

@app.route('/process_files', methods=['POST'])
def process_files():
    bucket_name = os.getenv('GCS_BUCKET', None)
    logging.info(f"bucket: {bucket_name}")

    uploaded_files = request.files.getlist('files')
    with tempfile.TemporaryDirectory() as temp_dir:
        vector_name = "edmonbrain"
        summaries = bot_help.handle_files(uploaded_files, temp_dir, vector_name)
        return jsonify({"summaries": summaries if summaries else ["No files were uploaded"]})

app_chat_history = []

@app.route('/process_input', methods=['POST'])
def process_input():
    # json input
    data = request.get_json()
    logging.info(f'Request data: {data}')

    user_input  = data.get('user_input', '')
    vector_name = 'edmonbrain' # replace with your vector name

    paired_messages = bot_help.extract_chat_history(app_chat_history)

    # ask the bot a question about the documents in the vectorstore
    bot_output = qs.qna(user_input, vector_name, chat_history=paired_messages)

    # append user message to chat history
    app_chat_history.append({'name': 'Human', 'content': user_input})
    
    # append bot message to chat history
    app_chat_history.append({'name': 'AI', 'content': bot_output['answer']})

    logging.info(f"bot_output: {bot_output}")

    return jsonify(bot_help.generate_output(bot_output))


@app.route('/discord/<vector_name>/message', methods=['POST'])
def discord_message(vector_name):
    data = request.get_json()
    user_input = data['content'].strip()  # Extract user input from the payload

    logging.info(f"discord_message: {data} to {vector_name}")

    chat_history = data.get('chat_history', None)
    paired_messages = bot_help.extract_chat_history(chat_history)

    command_response = bot_help.handle_special_commands(user_input, vector_name, paired_messages)
    if command_response is not None:
        return jsonify(command_response)

    bot_output = qs.qna(user_input, vector_name, chat_history=paired_messages)
    logging.info(f"bot_output: {bot_output}")
    
    discord_output = bot_help.generate_discord_output(bot_output)

    # may be over 4000 char limit for discord but discord bot chunks it up for output
    return jsonify(discord_output)

@app.route('/discord/<vector_name>/files', methods=['POST'])
def discord_files(vector_name):
    data = request.get_json()
    attachments = data.get('attachments', [])
    content = data.get('content', "").strip()
    chat_history = data.get('chat_history', [])

    logging.info(f'discord_files got data: {data}')
    with tempfile.TemporaryDirectory() as temp_dir:
        # Handle file attachments
        bot_output = []
        for attachment in attachments:
            # Download the file and store it temporarily
            file_url = attachment['url']
            file_name = attachment['filename']
            safe_file_name = os.path.join(temp_dir, file_name)
            response = requests.get(file_url)
            
            open(safe_file_name, 'wb').write(response.content)

            gs_file = bot_help.app_to_store(safe_file_name, 
                                            vector_name, 
                                            via_bucket_pubsub=True, 
                                            metadata={'discord_comment': content})
            bot_output.append(f"{file_name} uploaded to {gs_file}")

    # Format the response payload
    response_payload = {
        "summaries": bot_output
    }

    return response_payload, 200

# can only take up to 10 minutes to ack
@app.route('/pubsub_chunk_to_store/<vector_name>', methods=['POST'])
def pubsub_chunk_to_store(vector_name):
    """
    Final PubSub destination for each chunk that sends data to Supabase vectorstore"""
    if request.method == 'POST':
        data = request.get_json()

        meta = pb.from_pubsub_to_supabase(data, vector_name)

        return {'status': 'Success'}, 200


@app.route('/pubsub_to_store/<vector_name>', methods=['POST'])
def pubsub_to_store(vector_name):
    """
    splits up text or gs:// file into chunks and sends to pubsub topic 
      that pushes back to /pubsub_chunk_to_store/<vector_name>
    """
    if request.method == 'POST':
        data = request.get_json()

        meta = pbembed.data_to_embed_pubsub(data, vector_name)
        file_uploaded = str(meta.get("source", "Could not find a source"))
        return jsonify({'status': 'Success', 'source': file_uploaded}), 200

@app.route('/pubsub_to_discord', methods=['POST'])
def pubsub_to_discord():
    if request.method == 'POST':
        data = request.get_json()
        message_data = bot_help.process_pubsub(data)
        if isinstance(message_data, str):
            the_data = message_data
        elif isinstance(message_data, dict):
            if message_data.get('status', None) is not None:
                cloud_build_status = message_data.get('status')
                the_data = {'type': 'cloud_build', 'status': cloud_build_status}
                if cloud_build_status not in ['SUCCESS','FAILED']:
                    return cloud_build_status, 200

        response = bot_help.discord_webhook(the_data)

        if response.status_code != 204:
            logging.info(f'Request to discord returned {response.status_code}, the response is:\n{response.text}')
        
        return 'ok', 200

gchat_chat_history = []

@app.route('/gchat/<vector_name>/message', methods=['POST'])
def gchat_message(vector_name):
    event = request.get_json()
    logging.info(f'gchat_event: {event}')
    if event['type'] == 'ADDED_TO_SPACE' and not event['space'].get('singleUserBotDm', False):
        text = 'Thanks for adding me to "%s"! Use !help to get started' % (event['space']['displayName'] if event['space']['displayName'] else 'this chat')
  
        return jsonify({'text': text})
    
    elif event['type'] == 'MESSAGE':
    
        bot_name = bot_help.get_gchat_bot_name_from_event(event)
        user_input = event['message']['text']  # Extract user input from the payload
        user_input = user_input.replace(f'@{bot_name}','').strip()

        if event['message'].get('slash_command', None) is not None:
            response = bot_help.handle_slash_commands(event['message']['slash_command'])
            if response is not None:
                logging.info(f'Changing to vector_name: {vector_name} in response to slash_command')
                vector_name = response

        paired_messages = bot_help.extract_chat_history(gchat_chat_history)

        command_response = bot_help.handle_special_commands(user_input, vector_name, paired_messages)
        if command_response is not None:
            return jsonify({'text': command_response['result']})

        bot_output = qs.qna(user_input, vector_name, chat_history=paired_messages)
        # append user message to chat history
        gchat_chat_history.append({'name': 'Human', 'content': user_input})
        gchat_chat_history.append({'name': 'AI', 'content': bot_output['answer']})

        logging.info(f"gbot_output: {bot_output}")

        meta_card = bot_help.generate_google_chat_card(bot_output, how_many=1)

    else:
        return
    
    gchat_output = {'cards': meta_card['cards'] }

    # may be over 4000 char limit for discord but discord bot chunks it up for output
    return jsonify(gchat_output)

# https://github.com/slackapi/bolt-python/blob/main/examples/flask/app.py
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
sapp = App()


def process_slack_message(sapp, body, logger, thread_ts=None):
    logger.info(body)
    team_id = body.get('team_id', None)
    if team_id is None:
        raise ValueError('Team_id not specified')
    user_input = body.get('event').get('text').strip()

    user = body.get('event').get('user')
    bot_user = body.get('authorizations')[0].get('user_id')

    # Remove mention of the bot user from user_input
    bot_mention = f"<@{bot_user}>"
    user_input = user_input.replace(bot_mention, "").strip()

    vector_name = bot_help.get_slack_vector_name(team_id, bot_user)
    if vector_name is None:
        raise ValueError(f'Could not derive vector_name from slack_config and {team_id}, {bot_user}')
    
    logging.info(f'Slack vector_name: {vector_name}')

    chat_historys = sapp.client.conversations_replies(
        channel=body['event']['channel'],
        ts=thread_ts
    ) if thread_ts else sapp.client.conversations_history(
        channel=body['event']['channel']
    )

    messages = chat_historys['messages']
    logging.debug('messages: {}'.format(messages))
    paired_messages = bot_help.extract_chat_history(messages)

    command_response = bot_help.handle_special_commands(user_input, vector_name, paired_messages)
    if command_response is not None:
        payload = {
            "response": command_response,
            "thread_ts": thread_ts
        }
        pubsub_manager = pubsub.PubSubManager(vector_name, pubsub_topic=f"slack_response_{vector_name}")
        pubsub_manager.publish_message(json.dumps(payload))
        return

    logging.info(f'Sending from Slack: {user_input} to {vector_name}')
    # it just gets stuck here and never progresses further
    bot_output = qs.qna(user_input, vector_name, chat_history=paired_messages)
    logger.info(f"bot_output: {bot_output}")

    slack_output = bot_output.get("answer", "No answer available")

    payload = {
        "response": slack_output,
        "thread_ts": thread_ts,
        "channel_id": body['event']['channel']  # Add the channel ID to the payload
    }
    pubsub_manager = pubsub.PubSubManager(vector_name, pubsub_topic=f"slack_response_{vector_name}")
    sub_name = f"pubsub_slack_response_{vector_name}"

    sub_exists = pubsub_manager.subscription_exists(sub_name)

    if not sub_exists:
        pubsub_manager.create_subscription(sub_name, push_endpoint="/pubsub/slack-response")

    pubsub_manager.publish_message(json.dumps(payload))

@sapp.middleware  # or app.use(log_request)
def log_request(logger, body, next):
    logger.debug(body)
    return next()

@sapp.event("app_mention")
def handle_app_mention(ack, body, say, logger):
    ack()  # immediately acknowledge the event 
    thread_ts = body['event']['ts']  # The timestamp of the original message
    process_slack_message(sapp, body, logger, thread_ts)

@sapp.event("message")
def handle_direct_message(ack, body, say, logger):
    ack()  # immediately acknowledge the event 
    process_slack_message(sapp, body, logger)


shandler = SlackRequestHandler(sapp)
@app.route('/slack/message', methods=['POST'])
def slack():
    return shandler.handle(request)

@app.route('/pubsub/slack-response', methods=['POST'])
def pubsub_to_slack():
    # Parse incoming Pub/Sub message
    message = request.get_json()
    
    # Check message format
    if not all (k in message for k in ('thread_ts', 'channel_id', 'bot_response')):
        return 'Bad Request: Invalid Pub/Sub message format', 400

    # Extract relevant info from message
    thread_ts = message['thread_ts']
    channel_id = message['channel_id']
    bot_response = message['bot_response']

    # Send response to Slack using Bolt App's client
    try:
        sapp.client.chat_postMessage(
            channel=channel_id,
            text=bot_response,
            thread_ts=thread_ts  # Comment this line if you want to send the message outside a thread
        )
        return '', 204
    except Exception as e:
        logging.error(f'Error sending message to Slack: {str(e)}')
        return 'Internal Server Error', 500

   
# needs to be done via Mailgun API
@app.route('/email', methods=['POST'])
def receive_email():
    # The email data will be in the request.form dictionary.
    # The exact structure of the data depends on how your email
    # service sends it. Check the service's documentation for details.
    email_data = request.form
    print(email_data)

    # Here you can add code to process the email data.

    return '', 200

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

