"""Fase 0: verificar captura WASAPI loopback con pyaudiowpatch.

Captura 3 segundos del loopback del dispositivo de salida por defecto
e informa si llego audio (reproduce algo con sonido mientras corre).
"""
import numpy as np
import pyaudiowpatch as pyaudio

p = pyaudio.PyAudio()
try:
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    print(f"Salida por defecto: {default_out['name']}")

    loopback = None
    if not default_out.get("isLoopbackDevice"):
        for dev in p.get_loopback_device_info_generator():
            if default_out["name"] in dev["name"]:
                loopback = dev
                break
    else:
        loopback = default_out

    if loopback is None:
        print("FALLO: no se encontro dispositivo loopback asociado a la salida por defecto")
        raise SystemExit(1)

    rate = int(loopback["defaultSampleRate"])
    channels = int(loopback["maxInputChannels"])
    print(f"Loopback: [{loopback['index']}] {loopback['name']} ({channels} ch, {rate} Hz)")

    frames = []
    def callback(in_data, frame_count, time_info, status):
        frames.append(np.frombuffer(in_data, dtype=np.int16))
        return (None, pyaudio.paContinue)

    print("Capturando 3 segundos (con tono de prueba autogenerado)...")
    import io, threading, time, wave, winsound
    tone_buf = io.BytesIO()
    with wave.open(tone_buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        t = np.arange(48000 * 2) / 48000.0
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).tobytes())
    threading.Thread(target=lambda: winsound.PlaySound(tone_buf.getvalue(), winsound.SND_MEMORY), daemon=True).start()
    stream = p.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                    input=True, input_device_index=loopback["index"],
                    frames_per_buffer=1024, stream_callback=callback)
    time.sleep(3)
    stream.stop_stream()
    stream.close()
finally:
    p.terminate()

if not frames:
    print("FALLO: no llego ningun bloque de audio")
    raise SystemExit(1)

data = np.concatenate(frames).astype(np.float32) / 32768.0
rms = float(np.sqrt(np.mean(data**2)))
peak = float(np.abs(data).max())
print(f"OK: {len(data)} muestras, RMS={rms:.5f}, pico={peak:.5f}")
print("Loopback FUNCIONA" + (" (se detecto senal de audio)" if rms > 1e-4 else " (stream abierto, pero silencio: reproduce algo y repite)"))
