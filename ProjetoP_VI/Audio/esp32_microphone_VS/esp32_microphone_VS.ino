/* 
  esp32_microphone_VS.ino (rev C - memória reduzida)
  --------------------------------------------------
  - Usa APENAS 1 buffer float (em PSRAM quando disponível)
  - Converte int16 -> float DURANTE a leitura da Serial (sem buffer int16 gigante)
  - Ajuda a evitar overflow de DRAM (.dram0_0_seg)

  Dicas de compilação/placa:
    * Em Boards Manager: selecione sua placa correta (ESP32-S3 se for o caso)
    * Tools -> PSRAM: Enabled
    * Opcional: Partition Scheme "Huge APP" (não interfere em DRAM, mas ajuda no flash)

  Protocolo PC -> ESP32 (Serial 921600):
    :AUDIO <id> <sr> <nsamples> <bits>\n
    + payload RAW int16 LE mono
*/

#include <Arduino.h>

// ======================== Edge Impulse ========================
// ajuste o nome caso seu header seja diferente
#include <Project_inferencing.h>

#if __has_include(<esp_heap_caps.h>)
#include <esp_heap_caps.h>
#endif

// ======================== Configurações ========================

static const uint32_t SERIAL_BAUD = 921600;
static const uint32_t SERIAL_HEADER_TIMEOUT_MS  = 5000;
static const uint32_t SERIAL_PAYLOAD_TIMEOUT_MS = 20000; // payload pode ser grande

// Buffer de áudio: float* (alocado em PSRAM se possível)
static float *audio_buf_f32 = nullptr;

// ======================== LED RGB ========================
#define RGB_BRIGHTNESS 127 // Change white brightness (max 255)
static uint8_t RED   = 0;
static uint8_t GREEN = 0;
static uint8_t BLUE  = 0;
static const float limiar = 0.80f;

// ======================== Utilidades Serial ====================

static bool wait_for_line(String &out, uint32_t timeout_ms) {
  uint32_t start = millis();
  out = "";
  while (millis() - start < timeout_ms) {
    while (Serial.available()) {
      char c = (char)Serial.read();
      if (c == '\n') return true;
      if (c != '\r') out += c;
    }
    delay(1);
  }
  return false;
}

// lê exatamente N bytes com timeout
static bool read_exact(uint8_t *dst, size_t n, uint32_t timeout_ms) {
  uint32_t start = millis();
  size_t got = 0;
  while (got < n && (millis() - start) < timeout_ms) {
    int avail = Serial.available();
    if (avail > 0) {
      size_t to_read = (size_t)min((size_t)avail, n - got);
      int r = Serial.readBytes((char*)dst + got, (int)to_read);
      if (r > 0) got += (size_t)r;
    } else {
      delay(1);
    }
  }
  return got == n;
}

// Header: ":AUDIO <id> <sr> <nsamples> <bits>"
static bool parse_header(const String &line, String &id, uint32_t &sr, uint32_t &nsamples, uint16_t &bits) {
  if (!line.startsWith(":AUDIO ")) return false;

  int sp = line.indexOf(' ');
  if (sp < 0) return false;
  String rest = line.substring(sp + 1);
  rest.trim();

  int p1 = rest.indexOf(' ');
  int p2 = rest.indexOf(' ', p1 + 1);
  int p3 = rest.indexOf(' ', p2 + 1);
  if (p1 < 0 || p2 < 0 || p3 < 0) return false;

  id       = rest.substring(0, p1);
  sr       = (uint32_t)rest.substring(p1 + 1, p2).toInt();
  nsamples = (uint32_t)rest.substring(p2 + 1, p3).toInt();
  bits     = (uint16_t)rest.substring(p3 + 1).toInt();

  if (bits != 16) return false;
  if (sr == 0 || nsamples == 0) return false;
  return true;
}

// Garante alocação do buffer float (PSRAM se disponível)
static bool ensure_audio_buffer(size_t n_samples) {
  if (audio_buf_f32) return true;
  size_t nbytes = n_samples * sizeof(float);

  // Tenta PSRAM se disponível
#if defined(BOARD_HAS_PSRAM) || defined(CONFIG_SPIRAM) || defined(CONFIG_SPIRAM_SUPPORT)
  audio_buf_f32 = (float*)heap_caps_malloc(nbytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!audio_buf_f32) {
    audio_buf_f32 = (float*)ps_malloc(nbytes); // fallback Arduino ps_malloc
  }
#endif
  // Se ainda não conseguiu, tenta DRAM comum
  if (!audio_buf_f32) {
    #if __has_include(<esp_heap_caps.h>)
    audio_buf_f32 = (float*)heap_caps_malloc(nbytes, MALLOC_CAP_8BIT);
    #else
    audio_buf_f32 = (float*)malloc(nbytes);
    #endif
  }

  if (!audio_buf_f32) {
    ei_printf("ERR: Falha ao alocar %.1f KB para audio_buf_f32\n", (float)nbytes / 1024.0f);
    return false;
  }
  return true;
}

// Recebe o frame e converte on-the-fly int16 -> float em [-1,1)
static bool receive_audio_frame(String &id_out) {
  String header;
  if (!wait_for_line(header, SERIAL_HEADER_TIMEOUT_MS)) {
    ei_printf("{\"error\":\"timeout_header\"}\n");
    return false;
  }

  String id;
  uint32_t sr = 0, nsamples = 0;
  uint16_t bits = 0;
  if (!parse_header(header, id, sr, nsamples, bits)) {
    ei_printf("{\"error\":\"bad_header\"}\n");
    return false;
  }

#if defined(EI_CLASSIFIER_FREQUENCY)
  if (sr != (uint32_t)EI_CLASSIFIER_FREQUENCY) {
    ei_printf("{\"error\":\"sr_mismatch\",\"expected\":%d,\"got\":%d}\n",
              EI_CLASSIFIER_FREQUENCY, sr);
    return false;
  }
#endif
  if (nsamples != (uint32_t)EI_CLASSIFIER_RAW_SAMPLE_COUNT) {
    ei_printf("{\"error\":\"nsamples_mismatch\",\"expected\":%d,\"got\":%d}\n",
              EI_CLASSIFIER_RAW_SAMPLE_COUNT, nsamples);
    return false;
  }

  if (!ensure_audio_buffer(nsamples)) {
    ei_printf("{\"error\":\"no_mem\"}\n");
    return false;
  }

  // Leitura por blocos e conversão direta
  const size_t bytes_total = (size_t)nsamples * sizeof(int16_t);
  const size_t CHUNK = 1024; // bytes, múltiplo de 2
  uint8_t tmp[CHUNK];
  size_t bytes_read = 0;
  size_t sample_index = 0;

  while (bytes_read < bytes_total) {
    size_t remaining = bytes_total - bytes_read;
    size_t to_read = remaining < CHUNK ? remaining : CHUNK;
    if (!read_exact(tmp, to_read, SERIAL_PAYLOAD_TIMEOUT_MS)) {
      ei_printf("{\"error\":\"payload_timeout\"}\n");
      return false;
    }
    // Converter este bloco
    size_t ns = to_read / 2;
    for (size_t i = 0; i < ns; ++i) {
      int16_t v = (int16_t)( (int)tmp[2*i] | ((int)tmp[2*i + 1] << 8) ); // little-endian
      audio_buf_f32[sample_index + i] = (float)v / 32768.0f;
    }
    sample_index += ns;
    bytes_read   += to_read;
  }

  id_out = id;
  return true;
}

// ======================== Arduino setup/loop ===================

void setup() {
  Serial.begin(SERIAL_BAUD);
  uint32_t t0 = millis();
  digitalWrite(RGB_BUILTIN, LOW);  // Turn the RGB LED off
  while (!Serial && (millis() - t0) < 3000) { delay(10); }

  ei_printf("Edge Impulse - Serial Audio Inference (rev C, PSRAM-friendly)\n");

  ei_printf("Inferencing settings:\n");
  ei_printf("\tInterval: ");
  ei_printf_float((float)EI_CLASSIFIER_INTERVAL_MS);
  ei_printf(" ms.\n");
  ei_printf("\tFrame size: %d\n", EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE);
  ei_printf("\tSample length: %d ms.\n", (int)( (EI_CLASSIFIER_RAW_SAMPLE_COUNT * 1000) / (int)EI_CLASSIFIER_FREQUENCY ));
  ei_printf("\tNo. of classes: %d\n",
            (int)(sizeof(ei_classifier_inferencing_categories) / sizeof(ei_classifier_inferencing_categories[0])));

#if defined(EI_CLASSIFIER_INIT)
  EI_IMPULSE_ERROR init_status = run_classifier_init();
  if (init_status != EI_IMPULSE_OK) {
    ei_printf("ERR: run_classifier_init() = %d\n", init_status);
  }
#endif

  ei_printf("Aguardando frames ':AUDIO <id> <sr> <nsamples> <bits>'...\n");
}

void loop() {
  String req_id;
  if (!receive_audio_frame(req_id)) {
    // erro já impresso; tenta novamente
    return;
  }

  // Monta o signal_t a partir do buffer float
  signal_t signal;
  int err = numpy::signal_from_buffer(audio_buf_f32, EI_CLASSIFIER_RAW_SAMPLE_COUNT, &signal);
  if (err != 0) {
    ei_printf("{\"id\":\"%s\",\"error\":\"signal_build\",\"code\":%d}\n", req_id.c_str(), err);
    return;
  }

  ei_impulse_result_t result = { 0 };
  bool debug_nn = false;
  EI_IMPULSE_ERROR rc = run_classifier(&signal, &result, debug_nn);
  if (rc != EI_IMPULSE_OK) {
    ei_printf("{\"id\":\"%s\",\"error\":\"inference\",\"code\":%d}\n", req_id.c_str(), rc);
    return;
  }

  //LED RGB
  if (result.classification[0].value>limiar) {      //Acender
    RED=random(100, 200);
    GREEN=random(100, 200);
    BLUE=random(100, 200);
    neopixelWrite(RGB_BUILTIN, RED, GREEN, BLUE);
  }else if (result.classification[1].value>limiar){ //Apagar
    RED=0;
    GREEN=0;
    BLUE=0;
    neopixelWrite(RGB_BUILTIN, RED, GREEN, BLUE);
  }else{                                            //Esperar
    neopixelWrite(RGB_BUILTIN, RGB_BRIGHTNESS, RGB_BRIGHTNESS, RGB_BRIGHTNESS);
    delay(200);
    neopixelWrite(RGB_BUILTIN, 0, 0, 0);
    delay(200);
    neopixelWrite(RGB_BUILTIN, RGB_BRIGHTNESS, RGB_BRIGHTNESS, RGB_BRIGHTNESS);
    delay(200);
    neopixelWrite(RGB_BUILTIN, 0, 0, 0);
    delay(200);
    neopixelWrite(RGB_BUILTIN, RGB_BRIGHTNESS, RGB_BRIGHTNESS, RGB_BRIGHTNESS);
    delay(200);
    neopixelWrite(RGB_BUILTIN, 0, 0, 0);
    delay(200);
    neopixelWrite(RGB_BUILTIN, RED, GREEN, BLUE);
  }

  // JSON
  ei_printf("{\"id\":\"%s\",\"timing_ms\":{\"dsp\":%lu,\"inf\":%lu,\"an\":%lu},\"pred\":[",
            req_id.c_str(),
            (unsigned long)result.timing.dsp,
            (unsigned long)result.timing.classification,
            (unsigned long)result.timing.anomaly);

  for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    ei_printf("%s{\"%s\":", (i ? "," : ""), result.classification[i].label);
    ei_printf_float(result.classification[i].value);
    ei_printf("}");
  }
#if EI_CLASSIFIER_HAS_ANOMALY == 1
  ei_printf(",{\"anomaly\":");
  ei_printf_float(result.anomaly);
  ei_printf("}");
#endif
  ei_printf("]}\n");
}
