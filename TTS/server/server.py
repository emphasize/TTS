#!flask/bin/python
import argparse
import io
import json
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Union

from flask import Flask, render_template, request, send_file
import xdg.BaseDirectory

from TTS.config import load_config
from TTS.utils.manage import ModelManager
from TTS.utils.synthesizer import Synthesizer

# if no ENV Variable is set default to ~/.local/share
CONFIG_PATH = os.path.join(xdg.BaseDirectory.xdg_data_home, 'tts', 'conf.json')

# integrate custom config
_custom_config = {}
if os.path.exists(CONFIG_PATH):
    _custom_config = load_config(CONFIG_PATH)

def create_argparser():
    def convert_boolean(x):
        return x.lower() in ["true", "1", "yes"]

    parser = argparse.ArgumentParser()
    parser.add_argument("--list_models", type=convert_boolean, nargs="?", const=True,
                        help="list available pre-trained tts and vocoder models.",
                        default=_custom_config.get("list_models", False))

    parser.add_argument("--model_name", type=str,
                        help="Name of one of the pre-trained tts models in format <language>/<dataset>/<model_name>",
                        default = _custom_config.get("model_name", "tts_models/en/ljspeech/tacotron2-DDC"))

    parser.add_argument("--vocoder_name", type=str, help="name of one of the released vocoder models.",
                        default=_custom_config.get("vocoder_name", None))

    # Args for running custom models
    parser.add_argument("--config_path", type=str, help="Path to model config file.",
                        default=_custom_config.get("config_path", None))

    parser.add_argument("--model_path", type=str, help="Path to model file.",
                        default=_custom_config.get("model_path", None))

    parser.add_argument("--vocoder_path", type=str,
                        help="Path to vocoder model file. If it is not defined, model uses GL as vocoder. \
                        Please make sure that you installed vocoder library before (WaveRNN).",
                        default=_custom_config.get("vocoder_path", None))

    parser.add_argument("--vocoder_config_path", type=str, help="Path to vocoder model config file.",
                        default=_custom_config.get("vocoder_config_path", None))

    parser.add_argument("--speakers_file_path", type=str, help="JSON file for multi-speaker model.",
                        default=_custom_config.get("speakers_file_path", None))

    parser.add_argument("--port", type=int, default=_custom_config.get("port", 5002), help="port to listen on.")

    parser.add_argument("--use_cuda", type=convert_boolean, help="true to use CUDA.",
                        default=_custom_config.get("use_cuda", False))

    parser.add_argument("--debug", type=convert_boolean, help="true to enable Flask debug mode.",
                        default=_custom_config.get("debug", False))

    parser.add_argument("--show_details", type=convert_boolean, help="Generate model detail page.",
                        default=_custom_config.get("show_details", False))

    return parser

# parse the args
args = create_argparser().parse_args()

path = Path(__file__).parent / "../.models.json"
manager = ModelManager(path)

if args.list_models:
    manager.list_models()
    sys.exit()

# update in-use models to the specified released models.
model_path = None
config_path = None
speakers_file_path = None
vocoder_path = None
vocoder_config_path = None

# CASE1: list pre-trained TTS models
if args.list_models:
    manager.list_models()
    sys.exit()

# CASE2: load pre-trained model paths
if args.model_name is not None and not args.model_path:
    model_path, config_path, model_item = manager.download_model(args.model_name)
    args.vocoder_name = model_item["default_vocoder"] if args.vocoder_name is None else args.vocoder_name

if args.vocoder_name is not None and not args.vocoder_path:
    vocoder_path, vocoder_config_path, _ = manager.download_model(args.vocoder_name)

# CASE3: set custom model paths
if args.model_path is not None:
    model_path = args.model_path
    config_path = args.config_path
    speakers_file_path = args.speakers_file_path

if args.vocoder_path is not None:
    vocoder_path = args.vocoder_path
    vocoder_config_path = args.vocoder_config_path

# load models
synthesizer = Synthesizer(
    tts_checkpoint=model_path,
    tts_config_path=config_path,
    tts_speakers_file=speakers_file_path,
    tts_languages_file=None,
    vocoder_checkpoint=vocoder_path,
    vocoder_config=vocoder_config_path,
    encoder_checkpoint="",
    encoder_config="",
    use_cuda=args.use_cuda,
)

use_multi_speaker = hasattr(synthesizer.tts_model, "num_speakers") and (
    synthesizer.tts_model.num_speakers > 1 or synthesizer.tts_speakers_file is not None
)

speaker_manager = getattr(synthesizer.tts_model, "speaker_manager", None)
# TODO: set this from SpeakerManager
use_gst = synthesizer.tts_config.get("use_gst", False)
app = Flask(__name__)


def style_wav_uri_to_dict(style_wav: str) -> Union[str, dict]:
    """Transform an uri style_wav, in either a string (path to wav file to be use for style transfer)
    or a dict (gst tokens/values to be use for styling)

    Args:
        style_wav (str): uri

    Returns:
        Union[str, dict]: path to file (str) or gst style (dict)
    """
    if style_wav:
        if os.path.isfile(style_wav) and style_wav.endswith(".wav"):
            return style_wav  # style_wav is a .wav file located on the server

        style_wav = json.loads(style_wav)
        return style_wav  # style_wav is a gst dictionary with {token1_id : token1_weigth, ...}
    return None


@app.route("/")
def index():
    return render_template(
        "index.html",
        show_details=args.show_details,
        use_multi_speaker=use_multi_speaker,
        speaker_ids=speaker_manager.name_to_id if speaker_manager is not None else None,
        use_gst=use_gst,
    )


@app.route("/details")
def details():
    model_config = load_config(config_path)
    if args.vocoder_config is not None and os.path.isfile(args.vocoder_config):
        vocoder_config = load_config(args.vocoder_config)
    else:
        vocoder_config = None

    return render_template(
        "details.html",
        show_details=args.show_details,
        model_config=model_config,
        vocoder_config=vocoder_config,
        args=args.__dict__,
    )


lock = Lock()


@app.route("/api/tts", methods=["GET"])
def tts():
    with lock:
        text = request.args.get("text")
        speaker_idx = request.args.get("speaker_id", "")
        style_wav = request.args.get("style_wav", "")
        style_wav = style_wav_uri_to_dict(style_wav)
        print(" > Model input: {}".format(text))
        print(" > Speaker Idx: {}".format(speaker_idx))
        wavs = synthesizer.tts(text, speaker_name=speaker_idx, style_wav=style_wav)
        out = io.BytesIO()
        synthesizer.save_wav(wavs, out)
    return send_file(out, mimetype="audio/wav")


def main():
    app.run(debug=args.debug, host="::", port=args.port)


if __name__ == "__main__":
    main()
