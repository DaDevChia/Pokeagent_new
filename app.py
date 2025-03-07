import os
# Force SDL audio to use a dummy driver (prevents sound initialization)
os.environ["SDL_AUDIODRIVER"] = "dummy"

import logging
# Mute sound logging from PyBoy (suppress CRITICAL messages)
logging.getLogger('pyboy.core.sound').handlers.clear()

import time
import json
import threading
from flask import Flask, jsonify, request, send_file, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from io import BytesIO

from pyboy import PyBoy
from PIL import Image
import base64

from pokemon_helper import PokemonHelper

class PokeAgent:
    def __init__(self, rom_path, window_type="SDL2"):
        self.rom_path = rom_path
        self.pyboy = None
        self.init_lock = threading.Event()
        self.emu_thread = threading.Thread(target=self._run_emulator,
                                           args=(rom_path, window_type),
                                           daemon=True)
        self.emu_thread.start()
        self.init_lock.wait()  # Wait until PyBoy is initialized

        try:
            self.pokemon_helper = PokemonHelper(self.pyboy)
        except Exception as e:
            print("Warning: Could not initialize Pokemon helper:", e)
            class DummyHelper:
                def __init__(self):
                    self.map_data = {}
                def is_game_loaded(self):
                    return False
                def get_map_location(self):
                    return {"error": "Pokemon helper not available"}
                def get_detailed_party(self):
                    return {"error": "Pokemon helper not available"}
                def get_walkable_tiles(self):
                    return {"error": "Pokemon helper not available"}
                def get_tiles(self):
                    return {"error": "Pokemon helper not available"}
                def navigate_to(self, *args, **kwargs):
                    return {"error": "Pokemon helper not available"}
            self.pokemon_helper = DummyHelper()

        # Memory constants (Pokemon Red)
        self.PLAYER_X = 0xD362
        self.PLAYER_Y = 0xD361
        self.MAP_ID = 0xD35E
        self.PARTY_SIZE = 0xD163
        self.BADGES = 0xD356
        self.CURRENT_HP_ADDRESSES = [0xD16C, 0xD198, 0xD1C4, 0xD1F0, 0xD21C, 0xD248]
        self.MAX_HP_ADDRESSES = [0xD18D, 0xD1B9, 0xD1E5, 0xD211, 0xD23D, 0xD269]
        self.POKEMON_LEVEL_ADDRESSES = [0xD18C, 0xD1B8, 0xD1E4, 0xD210, 0xD23C, 0xD268]
        self.POKEMON_SPECIES_ADDRESSES = [0xD164, 0xD165, 0xD166, 0xD167, 0xD168, 0xD169]
        self.EVENT_FLAGS_START = 0xD747
        self.EVENT_FLAGS_END = 0xD87E

        self.last_button = None

        self.event_names = {}
        if os.path.exists("events.json"):
            with open("events.json", "r") as f:
                self.event_names = json.load(f)

    def _run_emulator(self, rom_path, window_type):
        # Create PyBoy with sound disabled; SDL_AUDIODRIVER is already set to "dummy"
        self.pyboy = PyBoy(rom_path, window=window_type, debug=False, sound_emulated=True)
        self.pyboy.set_emulation_speed(1)
        self.pyboy.tick()  # Kick off initialization
        self.init_lock.set()  # Signal that PyBoy is ready
        while True:
            self.pyboy.tick()
            time.sleep(0.01)

    def tick(self, n=1):
        return self.pyboy.tick(n)

    def button(self, input_key, delay=1):
        valid_buttons = ["a", "b", "start", "select", "left", "right", "up", "down"]
        if input_key not in valid_buttons:
            raise ValueError(f"Invalid input: {input_key}. Must be one of {valid_buttons}")
        self.last_button = input_key
        self.pyboy.button(input_key)
        self.pyboy.tick(delay)
        return True

    def get_screen_image(self):
        return self.pyboy.screen.image.copy()

    def get_screen_base64(self):
        image = self.get_screen_image()
        buffered = BytesIO()
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')

    def get_player_position(self):
        x = self.pyboy.memory[self.PLAYER_X]
        y = self.pyboy.memory[self.PLAYER_Y]
        map_id = self.pyboy.memory[self.MAP_ID]
        return {"x": x, "y": y, "map_id": map_id}

    def read_hp(self, start_address):
        return 256 * self.pyboy.memory[start_address] + self.pyboy.memory[start_address + 1]

    def get_party_info(self):
        party_size = self.pyboy.memory[self.PARTY_SIZE]
        pokemon = []
        for i in range(min(party_size, 6)):
            species_id = self.pyboy.memory[self.POKEMON_SPECIES_ADDRESSES[i]]
            level = self.pyboy.memory[self.POKEMON_LEVEL_ADDRESSES[i]]
            current_hp = self.read_hp(self.CURRENT_HP_ADDRESSES[i])
            max_hp = self.read_hp(self.MAX_HP_ADDRESSES[i])
            pokemon.append({
                "species_id": species_id,
                "level": level,
                "current_hp": current_hp,
                "max_hp": max_hp,
                "hp_percent": current_hp / max(max_hp, 1) * 100
            })
        return {"party_size": party_size, "pokemon": pokemon}

    def get_badges(self):
        badges_byte = self.pyboy.memory[self.BADGES]
        badges = []
        badge_names = ["Boulder", "Cascade", "Thunder", "Rainbow", "Soul", "Marsh", "Volcano", "Earth"]
        for i in range(8):
            if (badges_byte >> i) & 1:
                badges.append(badge_names[i])
        return {"badges_byte": badges_byte, "badges": badges, "count": len(badges)}

    def read_bit(self, addr, bit):
        return bin(256 + self.pyboy.memory[addr])[-bit - 1] == "1"

    def get_event_flags(self):
        event_flags = {}
        for address in range(self.EVENT_FLAGS_START, self.EVENT_FLAGS_END):
            val = self.pyboy.memory[address]
            for idx in range(8):
                if (val >> idx) & 1:
                    key = f"0x{address:X}-{idx}"
                    flag_name = self.event_names.get(key, "Unknown")
                    event_flags[key] = flag_name
        return event_flags

    def get_game_state(self):
        return {
            "position": self.get_player_position(),
            "party": self.get_party_info(),
            "badges": self.get_badges(),
            "events": self.get_event_flags(),
            "last_button": self.last_button
        }

    def move_to_location(self, target_x, target_y, max_steps=100):
        steps_taken = 0
        current_pos = self.get_player_position()
        while (current_pos["x"] != target_x or current_pos["y"] != target_y) and steps_taken < max_steps:
            if current_pos["x"] < target_x:
                self.button("right")
            elif current_pos["x"] > target_x:
                self.button("left")
            elif current_pos["y"] < target_y:
                self.button("down")
            elif current_pos["y"] > target_y:
                self.button("up")
            steps_taken += 1
            current_pos = self.get_player_position()
        return steps_taken < max_steps

    def close(self):
        if self.pyboy:
            self.pyboy.stop()

# Flask setup
app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Define your Flask routes (unchanged)
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/controller')
def controller():
    return render_template('controller.html')

@app.route('/api/screen', methods=['GET'])
def get_screen():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    fmt = request.args.get('format', 'base64')
    if fmt == 'base64':
        img_data = agent.get_screen_base64()
        return jsonify({"image": img_data})
    else:
        img = agent.get_screen_image()
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')

@app.route('/api/button', methods=['POST'])
def press_button():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    data = request.json
    button = data.get('button')
    delay = data.get('delay', 1)
    try:
        result = agent.button(button, delay)
        socketio.emit('button_press', {'button': button})
        return jsonify({"status": "success", "button": button, "result": result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/state', methods=['GET'])
def get_state():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.get_game_state())

@app.route('/api/position', methods=['GET'])
def get_position():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.get_player_position())

@app.route('/api/party', methods=['GET'])
def get_party():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.get_party_info())

@app.route('/api/badges', methods=['GET'])
def get_badges():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.get_badges())

@app.route('/api/events', methods=['GET'])
def get_events():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.get_event_flags())

@app.route('/api/move', methods=['POST'])
def move_to_location():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    data = request.json
    target_x = data.get('x')
    target_y = data.get('y')
    max_steps = data.get('max_steps', 100)
    if target_x is None or target_y is None:
        return jsonify({"error": "Target coordinates required"}), 400
    if agent.pokemon_helper.is_game_loaded():
        result = agent.pokemon_helper.navigate_to(target_x, target_y, max_steps)
        return jsonify(result)
    else:
        result = agent.move_to_location(target_x, target_y, max_steps)
        return jsonify({
            "status": "success" if result else "failure",
            "reached_destination": result,
            "current_position": agent.get_player_position()
        })

@app.route('/api/tick', methods=['POST'])
def tick():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    data = request.json
    n = data.get('n', 1)
    agent.tick(n)
    return jsonify({"status": "success", "ticks": n})

@app.route('/api/pokemon/location', methods=['GET'])
def get_pokemon_location():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.pokemon_helper.get_map_location())

@app.route('/api/pokemon/party', methods=['GET'])
def get_pokemon_party():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify(agent.pokemon_helper.get_detailed_party())

@app.route('/api/pokemon/tiles', methods=['GET'])
def get_pokemon_tiles():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    return jsonify({
        "walkable": agent.pokemon_helper.get_walkable_tiles(),
        "tiles": agent.pokemon_helper.get_tiles()
    })

@app.route('/api/pokemon/maps', methods=['GET'])
def get_pokemon_maps():
    if not agent:
        return jsonify({"error": "PokeAgent not initialized"}), 400
    if not hasattr(agent.pokemon_helper, 'map_data'):
        return jsonify({"error": "Map data not available"}), 400
    return jsonify(agent.pokemon_helper.map_data)

@socketio.on('connect')
def handle_connect():
    emit('connection_status', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    pass

import atexit
@atexit.register
def cleanup():
    if agent:
        agent.close()

if __name__ == '__main__':
    agent = PokeAgent("PokemonRed.gb", window_type="SDL2")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False)