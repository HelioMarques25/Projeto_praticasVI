// Captura de fotos pelo cabo USB (Serial), SEM Wi-Fi.
// O PC manda "f" e a placa responde com a foto em JPEG:
//   START_IMAGE
//   <bytes do JPEG>
//   END_IMAGE
// (mesmo protocolo do slide 25 da aula)

#include "esp_camera.h"

#define BAUD 115200

// Pinos ESP32-S3 (CAMERA_MODEL_ESP32S3_EYE), iguais ao slide 24
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  15
#define SIOD_GPIO_NUM   4
#define SIOC_GPIO_NUM   5
#define Y2_GPIO_NUM    11
#define Y3_GPIO_NUM     9
#define Y4_GPIO_NUM     8
#define Y5_GPIO_NUM    10
#define Y6_GPIO_NUM    12
#define Y7_GPIO_NUM    18
#define Y8_GPIO_NUM    17
#define Y9_GPIO_NUM    16
#define VSYNC_GPIO_NUM  6
#define HREF_GPIO_NUM   7
#define PCLK_GPIO_NUM  13

bool camera_ok = false;

bool iniciarCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk  = XCLK_GPIO_NUM;
  c.pin_pclk  = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href  = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn  = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAMESIZE_QVGA;   // 320x240, igual à inferência
  c.jpeg_quality = 12;               // igual ao exemplo do Edge Impulse
  c.fb_count     = 1;
  c.fb_location  = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  c.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("ERRO,camera_init,0x%x\n", err);
    return false;
  }

  // Se a imagem sair invertida, descomente (e faça o MESMO no firmware de inferência):
  // sensor_t* s = esp_camera_sensor_get();
  // s->set_vflip(s, 1);
  // s->set_hmirror(s, 1);
  return true;
}

void enviarFoto() {
  if (!camera_ok) {
    Serial.println("ERRO,camera_nao_iniciada");
    return;
  }

  // Descarta o frame que estava parado no buffer (pode ser antigo)
  camera_fb_t* fb = esp_camera_fb_get();
  if (fb) esp_camera_fb_return(fb);

  fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("ERRO,captura_falhou");
    return;
  }

  Serial.println("START_IMAGE");
  Serial.write(fb->buf, fb->len);
  Serial.println();
  Serial.println("END_IMAGE");
  Serial.flush();

  esp_camera_fb_return(fb);
}

void setup() {
  Serial.begin(BAUD);
  delay(1500);

  Serial.println("Captura via Serial");
  Serial.printf("PSRAM: %s\n", psramFound() ? "OK" : "NAO ENCONTRADA");

  camera_ok = iniciarCamera();
  if (camera_ok) Serial.println("READY");
  else Serial.println("Confira cabo flat da camera e PSRAM = OPI PSRAM em Ferramentas");
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "f") enviarFoto();
    else if (cmd == "ping") Serial.println(camera_ok ? "READY" : "ERRO,camera_nao_iniciada");
  }
}
