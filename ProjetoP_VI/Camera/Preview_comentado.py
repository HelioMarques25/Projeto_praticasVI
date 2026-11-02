"""
Preview_comentado.py — Visualizador simples para ESP32‑S3‑CAM (Edge Impulse)
----------------------------------------------------------------------------

O QUE ESTE ARQUIVO FAZ
- Abre a porta serial do ESP32‑S3 (115200).
- Envia "f\n" quando você clica em "Capturar (f)" ou ativa o "Preview".
- Espera um frame JPEG delimitado por:
    START_IMAGE\n
    <bytes JPEG>
    END_IMAGE\r\n
- Decodifica o JPEG e mostra a imagem em uma janela.
- Mostra também as predições impressas pelo firmware do Edge (“Predictions ...”).

COMO ADAPTAR O CÓDIGO ORIGINAL DO EDGE (MÍNIMO NECESSÁRIO)
1) No topo do .ino exportado pelo Edge, selecione a pinagem do ESP32‑S3‑EYE:
       //#define CAMERA_MODEL_ESP_EYE // Has PSRAM
       #define CAMERA_MODEL_ESP32S3_EYE // Has PSRAM
       //#define CAMERA_MODEL_AI_THINKER // Has PSRAM
   e mantenha o bloco de pinos correspondente (conforme sua placa).

2) Dentro de ei_camera_capture(...), logo após:
       camera_fb_t *fb = esp_camera_fb_get();
       if (!fb) { ... }
   acrescente o bloco que envia a imagem quando o PC pedir 'f':
       if (Serial.available()) {
           String comando = Serial.readStringUntil('\n');
           comando.trim();
           if (comando == "f") {
               Serial.println("START_IMAGE");
               Serial.write(fb->buf, fb->len);   // envia APENAS os bytes do JPEG
               Serial.println("END_IMAGE");
           }
       }
   (não altere o restante do fluxo do Edge — resize + run_classifier + prints "Predictions")

DICAS
- Não imprima nada entre START_IMAGE/END_IMAGE além dos bytes JPEG.
- Se a imagem aparecer invertida, troque FLIP_IMAGE=True/False aqui no script
  (ou ajuste set_vflip/set_hmirror no sensor do .ino).
- Se não aparecer imagem, confira a porta COM e aumente o timeout se preciso.
----------------------------------------------------------------------------
Dependências (no seu venv):
    pip install pyserial opencv-python pillow
"""

import threading
import time
import numpy as np
import cv2
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, messagebox

# pyserial
try:
    import serial
    from serial.tools import list_ports
except Exception as e:
    raise SystemExit("pyserial não encontrado. Instale com: pip install pyserial") from e

# ===================== CONFIGURAÇÃO =====================
BAUD       = 115200
TIMEOUT_S  = 2          # timeout de leitura serial (segundos)
FLIP_IMAGE = True       # True: corrige imagem "de cabeça para baixo"
PREVIEW_PERIOD_MS = 2000  # intervalo do preview automático (ms)
# ========================================================


class PreviewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Preview ESP32-S3-CAM (Edge) — Preview_comentado.py")
        self.geometry("920x680")
        self.resizable(True, True)

        self.ser = None
        self.preview_job = None
        self.reader_thread = None
        self.reader_running = False

        self._build_ui()
        self._set_connected(False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -------------------- UI --------------------
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        # Porta COM
        ttk.Label(top, text="Porta:").pack(side="left")
        self.port_var = tk.StringVar(value="")
        self.cb_port = ttk.Combobox(top, textvariable=self.port_var, width=12, state="readonly")
        self.cb_port.pack(side="left", padx=4)

        self.btn_refresh = ttk.Button(top, text="Atualizar", command=self._refresh_ports)
        self.btn_refresh.pack(side="left", padx=4)

        # Conectar / Desconectar
        self.btn_connect = ttk.Button(top, text="Conectar", command=self._connect)
        self.btn_connect.pack(side="left", padx=(12, 4))

        self.btn_disconnect = ttk.Button(top, text="Desconectar", command=self._disconnect)
        self.btn_disconnect.pack(side="left", padx=4)

        # Status
        self.lbl_status = ttk.Label(top, text="Status: desconectado")
        self.lbl_status.pack(side="right")

        # Linha 2: botões de ação
        top2 = ttk.Frame(self)
        top2.pack(fill="x", padx=8, pady=(0, 6))

        self.btn_capture = ttk.Button(top2, text="Capturar (f)", command=self._send_f)
        self.btn_capture.pack(side="left", padx=4)

        self.btn_prev_on = ttk.Button(top2, text="Iniciar Preview", command=self._start_preview)
        self.btn_prev_on.pack(side="left", padx=4)

        self.btn_prev_off = ttk.Button(top2, text="Parar Preview", command=self._stop_preview)
        self.btn_prev_off.pack(side="left", padx=4)

        # Imagem
        self.img_label = ttk.Label(self, relief="sunken")
        self.img_label.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # Painel inferior: Predições + Log
        bottom = ttk.Frame(self)
        bottom.pack(fill="both", expand=True, padx=8, pady=6)

        # Predições
        fr_pred = ttk.LabelFrame(bottom, text="Predições (texto impresso pelo firmware)")
        fr_pred.pack(side="left", fill="both", expand=True, padx=(0, 4))

        self.txt_pred = tk.Text(fr_pred, height=10, wrap="none")
        self.txt_pred.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_pred.insert("end", "Aguardando 'Predictions' do firmware...\n")
        self.txt_pred.configure(state="disabled")

        # Log bruto
        fr_log = ttk.LabelFrame(bottom, text="Log Serial (bruto)")
        fr_log.pack(side="left", fill="both", expand=True, padx=(4, 0))

        self.txt_log = tk.Text(fr_log, height=10, wrap="none")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_log.configure(state="disabled")

        # popula portas na inicialização
        self._refresh_ports()

    def _refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.cb_port["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _set_connected(self, is_conn: bool):
        # habilita/desabilita botões de captura/preview conforme conexão
        state_on = "normal" if is_conn else "disabled"
        self.btn_capture.configure(state=state_on)
        self.btn_prev_on.configure(state=state_on)
        self.btn_prev_off.configure(state=state_on)

        # conectar/desconectar
        self.btn_connect.configure(state=("disabled" if is_conn else "normal"))
        self.btn_disconnect.configure(state=("normal" if is_conn else "disabled"))
        self.btn_refresh.configure(state=("disabled" if is_conn else "normal"))
        self.cb_port.configure(state=("disabled" if is_conn else "readonly"))

        self.lbl_status.configure(
            text=("Status: conectado" if is_conn else "Status: desconectado")
        )

    # -------------------- Serial: conectar/desconectar --------------------
    def _connect(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Erro", "Selecione uma porta COM.")
            return

        try:
            self.ser = serial.Serial(port, BAUD, timeout=TIMEOUT_S)
            time.sleep(1.0)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self._log(f"[OK] Conectado em {port} @ {BAUD}")
        except Exception as e:
            self.ser = None
            messagebox.showerror("Erro", f"Não foi possível abrir {port} @ {BAUD}:\n{e}")
            self._set_connected(False)
            return

        self._set_connected(True)

        # inicia thread de leitura só uma vez
        if not self.reader_thread or not self.reader_thread.is_alive():
            self.reader_running = True
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

    def _disconnect(self):
        self._stop_preview()
        self.reader_running = False

        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

        self.ser = None
        self._log("[OK] Desconectado")
        self._set_connected(False)

    # -------------------- Serial: envio --------------------
    def _send_f(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Aviso", "Conecte na porta serial antes.")
            return
        try:
            self.ser.write(b"f\n")
            self._log(">>> f")
        except Exception as e:
            self._log(f"[ERRO] Envio 'f': {e}")

    def _start_preview(self):
        if self.preview_job is None:
            def loop():
                self._send_f()
                self.preview_job = self.after(PREVIEW_PERIOD_MS, loop)
            loop()

    def _stop_preview(self):
        if self.preview_job:
            try:
                self.after_cancel(self.preview_job)
            except Exception:
                pass
            self.preview_job = None

    # -------------------- Serial: leitura --------------------
    def _reader_loop(self):
        in_pred_block = False
        pred_lines = []

        while self.reader_running:
            try:
                if self.ser is None or (not self.ser.is_open):
                    time.sleep(0.2)
                    continue

                line = self.ser.readline()
                if not line:
                    continue

                # START_IMAGE
                if b"START_IMAGE" in line:
                    blob = self.ser.read_until(b"END_IMAGE\r\n")
                    marker = b"END_IMAGE\r\n"
                    data = blob[:-len(marker)] if blob.endswith(marker) else blob.split(b"END_IMAGE")[0]
                    self._handle_image(data)
                    continue

                # Texto (logs/predições)
                text = line.decode("utf-8", errors="ignore").rstrip()
                if not text:
                    continue

                self._log(text)

                # Detecta bloco de predições
                if text.startswith("Predictions (DSP:"):
                    in_pred_block = True
                    pred_lines = [text]
                    continue

                if in_pred_block:
                    pred_lines.append(text)
                    if text.strip().startswith("End predictions"):
                        self._set_predictions(pred_lines)
                        in_pred_block = False
                        pred_lines = []

            except Exception as e:
                self._log(f"[ERRO] leitura: {e}")
                time.sleep(0.2)

    # -------------------- Handlers --------------------
    def _handle_image(self, jpg_bytes: bytes):
        try:
            buf = np.frombuffer(jpg_bytes, dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                self._log("[ERRO] imdecode falhou (JPEG inválido)")
                return
            if FLIP_IMAGE:
                img = cv2.flip(img, -1)
            self.after(0, self._update_image_label, img)
        except Exception as e:
            self._log(f"[ERRO] _handle_image: {e}")

    def _update_image_label(self, img_bgr: np.ndarray):
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w = img_rgb.shape[:2]

        # Redimensiona para caber na janela mantendo proporção
        max_w, max_h = 840, 420
        scale = min(max_w / w, max_h / h, 1.0)
        if scale < 1.0:
            new_size = (int(w * scale), int(h * scale))
            img_rgb = cv2.resize(img_rgb, new_size, interpolation=cv2.INTER_AREA)

        pil = Image.fromarray(img_rgb)
        tkimg = ImageTk.PhotoImage(pil)
        self.img_label.imgtk = tkimg
        self.img_label.configure(image=tkimg)

    def _set_predictions(self, lines):
        text = "\n".join(lines) + "\n"
        self.txt_pred.configure(state="normal")
        self.txt_pred.delete("1.0", "end")
        self.txt_pred.insert("end", text)
        self.txt_pred.configure(state="disabled")

    def _log(self, msg: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # -------------------- Fechamento --------------------
    def _on_close(self):
        self._stop_preview()
        self.reader_running = False
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = PreviewApp()
    app.mainloop()