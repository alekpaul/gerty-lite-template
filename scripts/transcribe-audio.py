#!/usr/bin/env python3
"""Transcribe audio/video files using faster-whisper with GPU acceleration.

Usage:
  python3 transcribe-audio.py <filepath>
  python3 transcribe-audio.py <filepath> --model large-v3
  python3 transcribe-audio.py <filepath> --device cpu

Supported formats: ogg, mp3, wav, flac, mp4, webm, m4a, opus
Outputs transcribed text to stdout.
Auto-detects language (optimized for Ukrainian + English).
"""
import sys
import os

# Hide all GPUs from ctranslate2/faster-whisper before any import.
# CUDA_VISIBLE_DEVICES="" prevents cublas64_12.dll load crash on systems
# where the CUDA driver exists but the matching cuBLAS DLL is missing.
os.environ['CUDA_VISIBLE_DEVICES'] = ''

# Force UTF-8 stdout on Windows (default cp1252 breaks Cyrillic/Ukrainian output)
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Add NVIDIA CUDA pip package DLLs to PATH (Windows)
if sys.platform == 'win32':
    try:
        import importlib.util
        for pkg in ['nvidia.cublas', 'nvidia.cudnn']:
            spec = importlib.util.find_spec(pkg)
            if spec and spec.submodule_search_locations:
                bin_dir = os.path.join(spec.submodule_search_locations[0], 'bin')
                if os.path.isdir(bin_dir):
                    os.add_dll_directory(bin_dir)
                    os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
    except Exception:
        pass


def transcribe(filepath, model_size='large-v3', device='cuda'):
    if not os.path.isfile(filepath):
        print(f'File not found: {filepath}', file=sys.stderr)
        sys.exit(1)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print('faster-whisper not installed. Run: pip install faster-whisper', file=sys.stderr)
        sys.exit(1)

    # Use float16 on GPU, int8 on CPU
    compute_type = 'float16' if device == 'cuda' else 'int8'

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        if device == 'cuda':
            print(f'CUDA failed ({e}), falling back to CPU...', file=sys.stderr)
            device = 'cpu'
            compute_type = 'int8'
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
        else:
            raise

    segments, info = model.transcribe(
        filepath,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    lang = info.language
    prob = info.language_probability
    print(f'[{lang} ({prob:.0%})]', file=sys.stderr)

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text.strip())

    full_text = ' '.join(text_parts)
    print(full_text)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: transcribe-audio.py <filepath> [--model NAME] [--device cpu|cuda]', file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    model_size = 'large-v3'
    device = 'cuda'

    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == '--model' and i + 1 < len(args):
            model_size = args[i + 1]
        elif arg == '--device' and i + 1 < len(args):
            device = args[i + 1]

    transcribe(filepath, model_size, device)
