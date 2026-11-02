#!/usr/bin/env python3
"""
gui_record_and_send.py
----------------------
App GUI (Tkinter) para:
  1) Gravar audio do microfone do PC ao clicar no botao
  2) Ajustar para 16 kHz, mono, 16-bit (1 s por padrao)
  3) Enviar para o ESP32 via Serial usando o protocolo:
       :AUDIO <id> <sr> <nsamples> <bits>\n  + payload RAW int16 LE
  4) Mostrar a resposta (JSON) do ESP32 na tela

Dependencias:
  pip install sounddevice pyserial numpy

Uso:
  python gui_record_and_send.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import json

import numpy as np

import sounddevice as sd
import serial
import serial.tools.list_ports as list_ports

DEFAULT_BAUD = 921600
TARGET_SR = 16000
TARGET_BITS = 16

def log(text_widget: tk.Text, msg: str):
    text_widget.configure(state="normal")
    text_widget.insert("end", msg + "\n")
    text_widget.see("end")
    text_widget.configure(state="disabled")

def list_serial_ports():
    return [p.device for p in list_ports.comports()]

def list_input_devices():
    devs = sd.query_devices()
    items = []
    for idx, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            sr = d.get("default_samplerate", None)
            label = f"[{idx}] {d['name']} - in_ch={d['max_input_channels']} sr={sr}"
            items.append((idx, label))
    return items

def resample_linear(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Resample 1D array x from sr_in to sr_out via simple linear interpolation."""
    if sr_in == sr_out:
        return x.copy()
    n_in = x.shape[0]
    n_out = int(round(n_in * (sr_out / sr_in)))
    xp = np.linspace(0.0, 1.0, n_in, endpoint=False, dtype=np.float64)
    xq = np.linspace(0.0, 1.0, n_out, endpoint=False, dtype=np.float64)
    y = np.interp(xq, xp, x.astype(np.float64)).astype(np.float32)
    return y

def record_audio(duration_s: float, dev_index: int, log_fn):
    """Record audio from microphone. Returns (sr, int16_mono_16k)."""
    if duration_s <= 0.0:
        raise ValueError("Duracao deve ser > 0")

    dev = sd.query_devices(dev_index)
    sr_in = int(dev.get("default_samplerate", TARGET_SR))
    ch = min(int(dev.get("max_input_channels", 1)), 2)

    log_fn(f"Mic: {dev['name']} | sr_in={sr_in} Hz | channels={ch} | dur={duration_s:.2f}s")

    frames = int(round(duration_s * sr_in))
    # grava float32 no range aproximado [-1, 1]
    x = sd.rec(frames, samplerate=sr_in, channels=ch, dtype="float32", device=dev_index)
    sd.wait()

    # mono
    if x.ndim == 2 and x.shape[1] > 1:
        x = np.mean(x, axis=1)
    else:
        x = x.reshape(-1)

    # resample p/ 16k
    x16 = resample_linear(x, sr_in, TARGET_SR)

    # normaliza p/ evitar clipping (mantém didático)
    peak = np.max(np.abs(x16)) + 1e-9
    if peak > 0.98:
        x16 = x16 / peak

    # float -> int16 LE
    x16_i16 = (x16 * 32767.0).astype(np.int16)

    return TARGET_SR, x16_i16

def read_line(ser: serial.Serial, timeout_s: float) -> str:
    ser.timeout = timeout_s
    line = ser.readline()
    if not line:
        return ""
    try:
        return line.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""

def send_over_serial(port: str, req_id: int, sr: int, x16_i16: np.ndarray, baud: int, timeout_s: float, log_fn):
    """
    Protocolo:
      PC -> ESP: ":AUDIO <id> <sr> <nsamples> <bits>\n" + payload int16 LE
      ESP -> PC: linhas de texto e por fim um JSON na mesma linha (ou texto)
    """
    ns = int(x16_i16.shape[0])
    bits = TARGET_BITS
    header = f":AUDIO {req_id} {sr} {ns} {bits}\n".encode("utf-8")
    payload = x16_i16.tobytes(order="C")

    log_fn(f"Serial: abrindo {port} @ {baud}")
    with serial.Serial(port, baudrate=baud, timeout=timeout_s) as ser:
        time.sleep(0.15)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        log_fn(f"Enviando header: :AUDIO {req_id} {sr} {ns} {bits}")
        ser.write(header)
        ser.flush()

        log_fn(f"Enviando payload: {len(payload)} bytes")
        ser.write(payload)
        ser.flush()

        # Lê respostas até receber JSON (linha começando com '{') ou até timeout acumulado
        t0 = time.time()
        last_text = ""
        while True:
            if time.time() - t0 > max(5.0, timeout_s * 3.0):
                log_fn("Timeout aguardando resposta do ESP.")
                return last_text

            line = read_line(ser, timeout_s=0.5)
            if line:
                last_text = line
                log_fn(f"<< {line}")
                if line.startswith("{") and line.endswith("}"):
                    return line

def safe_int(s, default=0):
    try:
        return int(s)
    except Exception:
        return default

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audio -> ESP32 (Edge Impulse) | 16 kHz | 1 s")
        self.geometry("950x620")

        # Top frame
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="Porta Serial:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value="")
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var, width=15, values=list_serial_ports())
        self.port_cb.grid(row=0, column=1, sticky="w", padx=6)

        self.btn_refresh_ports = ttk.Button(top, text="Atualizar", command=self.refresh_ports)
        self.btn_refresh_ports.grid(row=0, column=2, sticky="w", padx=6)

        ttk.Label(top, text="Baud:").grid(row=0, column=3, sticky="w", padx=(18, 0))
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, sticky="w", padx=6)

        ttk.Label(top, text="Timeout(s):").grid(row=0, column=5, sticky="w", padx=(18, 0))
        self.timeout_var = tk.StringVar(value="2.0")
        ttk.Entry(top, textvariable=self.timeout_var, width=7).grid(row=0, column=6, sticky="w", padx=6)

        ttk.Label(top, text="Mic:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.mic_var = tk.StringVar(value="")
        devs = list_input_devices()
        self.dev_map = {label: idx for idx, label in devs}
        self.mic_cb = ttk.Combobox(top, textvariable=self.mic_var, width=80, values=[lab for _, lab in devs])
        if devs:
            self.mic_var.set(devs[0][1])
        self.mic_cb.grid(row=1, column=1, columnspan=6, sticky="w", padx=6, pady=(10, 0))

        # Controls frame
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(ctrl, text="Duração (s):").grid(row=0, column=0, sticky="w")
        # ✅ padrão de aula = 1.0 s
        self.duration_var = tk.StringVar(value="1.0")
        ttk.Entry(ctrl, textvariable=self.duration_var, width=8).grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(ctrl, text="Req ID:").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.reqid_var = tk.StringVar(value="1")
        ttk.Entry(ctrl, textvariable=self.reqid_var, width=8).grid(row=0, column=3, sticky="w", padx=6)

        self.btn = ttk.Button(ctrl, text="Gravar (1s) e Enviar", command=self.on_click)
        self.btn.grid(row=0, column=4, sticky="w", padx=(18, 0))

        # Log frame
        frm_log = ttk.Frame(self)
        frm_log.pack(fill="both", expand=True, padx=10, pady=10)

        self.txt = tk.Text(frm_log, height=20, wrap="word", state="disabled")
        self.txt.pack(side="left", fill="both", expand=True)

        yscroll = ttk.Scrollbar(frm_log, orient="vertical", command=self.txt.yview)
        yscroll.pack(side="right", fill="y")
        self.txt.configure(yscrollcommand=yscroll.set)

        # Footer
        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(
            footer,
            text="Protocolo: envia ':AUDIO id sr nsamples bits' + RAW int16 LE. ESP responde logs + JSON.",
        ).pack(anchor="w")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Auto refresh ports
        self.refresh_ports()

    def refresh_ports(self):
        ports = list_serial_ports()
        self.port_cb["values"] = ports
        if not self.port_var.get() and ports:
            self.port_var.set(ports[0])

    def on_click(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Erro", "Selecione uma porta serial.")
            return

        mic_label = self.mic_var.get().strip()
        if mic_label not in self.dev_map:
            messagebox.showerror("Erro", "Selecione um microfone válido.")
            return

        dev_index = self.dev_map[mic_label]
        duration = float(self.duration_var.get())
        req_id = safe_int(self.reqid_var.get(), 1)
        baud = safe_int(self.baud_var.get(), DEFAULT_BAUD)
        timeout_s = float(self.timeout_var.get())

        self.btn.config(state="disabled")
        t = threading.Thread(target=self._do_record_send, args=(port, req_id, duration, dev_index, baud, timeout_s))
        t.daemon = True
        t.start()

    def _do_record_send(self, port, req_id, duration, dev_index, baud, timeout_s):
        try:
            log(self.txt, f"[{time.strftime('%H:%M:%S')}] Iniciando gravação...")
            sr, x16 = record_audio(duration, dev_index, lambda m: log(self.txt, m))
            log(self.txt, f"Audio OK: {x16.shape[0]} amostras @ {sr} Hz (int16).")

            resp = send_over_serial(port, req_id, sr, x16, baud, timeout_s, lambda m: log(self.txt, m))
            try:
                parsed = json.loads(resp)
                log(self.txt, f"Resposta (JSON): {json.dumps(parsed, ensure_ascii=False)}")
            except Exception:
                if resp:
                    log(self.txt, f"Resposta (texto): {resp}")
        except Exception as e:
            log(self.txt, f"ERRO: {e}")
        finally:
            self.btn.config(state="normal")

    def on_close(self):
        self.destroy()

if __name__ == "__main__":
    App().mainloop()
