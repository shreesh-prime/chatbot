from base64 import b64decode
import json
import re
import string
from channels.generic.websocket import WebsocketConsumer
import google.generativeai as genai
import requests
from cashx_apis.settings import GEMINI_API_KEY, MONGO_URI
from pymongo import MongoClient
from urllib.parse import parse_qs
import certifi
import uuid
import datetime
import time
from allosaurus.app import read_recognizer
from .prompts import kiosk_prompt, mobile_prompt, spanish_kiosk_prompt

class ChatConsumer(WebsocketConsumer):
	def connect(self):
		query_string = self.scope['query_string'].decode()
		query_params = parse_qs(query_string)
		self.platform = query_params.get('platform', [None])[0]
		self.lang = query_params.get('lang', [None])[0]

		self.init_chat()
		self.accept()
		self.send(text_data=json.dumps({'message': 'Connected'}))
	
	def receive(self, text_data):
		try:
			text_data_json = json.loads(text_data)
			message = text_data_json['message']
			chat_history = self.chat_collection.find_one({'session_id': self.session_id})
			chat_history['messages'].append({
				'role': 'user', 
				'message': message,
				'time': datetime.datetime.utcnow(),
			})

			# Get gemini chat response
			response = self.chat.send_message(message, stream=True)
			delimeters = ['.', '\n', ':', ';']
			self.send(text_data=json.dumps({'start': True}))
			full_response = '' 
			sentence = ''
			for chunk in response:
				sentence += chunk.text
				for delimeter in delimeters:
					if delimeter in chunk.text:
						self.send(text_data=json.dumps({'chunk': sentence, 'type': 'chat'}))
						full_response += sentence
						sentence = ''
			self.send(text_data=json.dumps({'chunk': sentence, 'end': True, 'type': 'chat'}))
			full_response += sentence

			filtered_response = filter_natural_language(full_response)
			i, j = 0, 80
			while i < len(filtered_response):
				if i > len(filtered_response) - 200:
					self.send_text_to_speech(filtered_response[i:])
					break
				while j < len(filtered_response)-1 and not filtered_response[j] in delimeters: j += 1
				self.send_text_to_speech(filtered_response[i: j+1])
				i = j + 2
				j += 100
				
			
			# Record message in database
			chat_history['messages'].append({
				'role': 'bot',
				'message': full_response,
				'time': datetime.datetime.utcnow(),
			})
			self.chat_collection.update_one({'session_id': self.session_id}, {'$set': chat_history})
		except Exception as e:
			self.send(text_data=json.dumps({'error': str(e), 'chunk': 'Error, please try again', 'type': 'chat', 'end': True}))
			print(str(e))


	def init_chat(self):
		genai.configure(api_key=GEMINI_API_KEY)

		generation_config = {
			"temperature": 0.3,
			"top_p": 0.95,
			"top_k": 64,
			"max_output_tokens": 8192,
			"response_mime_type": "text/plain",
		}
		print(self.lang)

		model = genai.GenerativeModel(
			model_name="gemini-1.5-flash",
			generation_config=generation_config,
			safety_settings=safety_settings_high,
			system_instruction="Be concise" + (' and reply in Spanish' if self.lang == 'es' else ''),
		)

		chat_session = model.start_chat(
			history=(
				mobile_prompt if self.platform == 'mobile' else (spanish_kiosk_prompt if self.lang == 'es' else kiosk_prompt)
      )
		)

		self.chat = chat_session

		client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
		self.session_id = str(uuid.uuid4())
		self.chat_collection = client['chatbot_db']['chat_history']
		chat_history = {'session_id': self.session_id, 'messages': []}
		self.chat_collection.insert_one(chat_history)

		self.phone_model = read_recognizer('latest')
		
	
	def send_text_to_speech(self, text):
		if (len(text) <= 1): return

		url = f'https://texttospeech.googleapis.com/v1/text:synthesize?key={GEMINI_API_KEY}'

		voice = {
			'languageCode': 'es-US', 
			'name': 'es-US-Neural2-A'
		} if self.lang == 'es' else {
			'languageCode': 'en-US',
			'name': 'en-US-Studio-O'
		}
		audio_config = {
			'audioEncoding': 'LINEAR16',
			'speakingRate': '1',
			'pitch': '-4'
		} if self.lang == 'es' else {
			'audioEncoding': 'LINEAR16',
			'speakingRate': '1',
		}

		headers = {
			'Content-Type': 'application/json',
		}
		payload = {
			'input': {'text': text},
			'voice': voice,
			'audioConfig': audio_config
		}
		response = requests.post(url, headers=headers, data=json.dumps(payload))

		if response.status_code == 200:
			audio_content = response.json().get('audioContent')
			with open('output.wav', 'wb') as out:
				out.write(b64decode(audio_content))
		else:
			print(f"Error: {response.status_code} \n{response.text}")
			self.send(text_data=json.dumps({'audio': '', 'mouthcues': '0 -1 p'}))
			return

		start = time.time()
		mouthcues = self.phone_model.recognize('output.wav', timestamp=True)
		print(time.time() - start)
		self.send(text_data=json.dumps({'audio': audio_content, 'mouthcues': format_mouthcues(mouthcues)}))
			

def filter_natural_language(text):
	navigate_pattern = r'NAVIGATE \[.*?\] \[.*?\]'
	filtered_text = re.sub(navigate_pattern, '', text)
	howto_pattern = r'HOWTO \[.*?\]'
	filtered_text = re.sub(howto_pattern, '', filtered_text)
	filtered_text = filtered_text.replace('CONTACTAGENT', '')
	last_curly = filtered_text.rfind('{')
	if last_curly > -1: filtered_text = filtered_text[:last_curly]
	allowed_characters = string.ascii_letters + string.digits + "'!,-.:;?/" + " \n"
	filtered_text = ''.join([char for char in filtered_text if char in allowed_characters])
	filtered_text = filtered_text.replace(':', '.')
	return filtered_text

def format_mouthcues(text: str):
	result = []
	rows = text.split('\n')
	for row in rows:
		items = row.split(' ')
		result.append([float(items[0]), float(items[1]), items[2]])
	return result


# Minimal safety settings so that Gemini doesn't block any responses
safety_settings_high = [
  {
    "category": "HARM_CATEGORY_HARASSMENT",
    "threshold": "BLOCK_ONLY_HIGH"
  },
  {
    "category": "HARM_CATEGORY_HATE_SPEECH",
    "threshold": "BLOCK_ONLY_HIGH"
  },
  {
    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "threshold": "BLOCK_ONLY_HIGH"

  },
  {
    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
    "threshold": "BLOCK_ONLY_HIGH"
  }
]