#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "lwip/inet.h"

#define WIFI_SSID ""// wifi to sniff
#define WIFI_PASS ""

#define SENSOR_NAME "room name"
#define SERVER_URL  "http://192.168.1.33:5000/update"

static const char *TAG = "TRACKER";

typedef struct {
    char mac[18];
    int rssi;
} sniff_msg_t;

static QueueHandle_t sniff_queue;
static bool got_ip = false;

static void wifi_event_handler(void *arg,
                               esp_event_base_t event_base,
                               int32_t event_id,
                               void *event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "WiFi started, connecting...");
        esp_wifi_connect();
    }
    else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
    wifi_event_sta_disconnected_t *event = (wifi_event_sta_disconnected_t *) event_data;
    got_ip = false;
    ESP_LOGW(TAG, "Disconnected, reason=%d, reconnecting...", event->reason);
    esp_wifi_connect();
    }
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *) event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        got_ip = true;
    }
}

static void wifi_init_sta(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    wifi_config_t wifi_config = {0};
    strcpy((char *)wifi_config.sta.ssid, WIFI_SSID);
    strcpy((char *)wifi_config.sta.password, WIFI_PASS);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

static void send_to_server(const char *mac_str, int rssi) {
    if (!got_ip) {
        ESP_LOGW(TAG, "No IP yet, skipping send");
        return;
    }

    esp_http_client_config_t config = {
        .url = SERVER_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 3000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);

    char json[128];
    snprintf(json, sizeof(json),
             "{\"sensor\":\"%s\",\"mac\":\"%s\",\"rssi\":%d}",
             SENSOR_NAME, mac_str, rssi);

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json, strlen(json));

    esp_err_t err = esp_http_client_perform(client);

    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "Sent %s RSSI=%d status=%d", mac_str, rssi, status);
    } else {
        ESP_LOGE(TAG, "HTTP send failed: %s", esp_err_to_name(err));
    }

    esp_http_client_cleanup(client);
}

static void sender_task(void *pv) {
    sniff_msg_t msg;

    while (1) {
        if (xQueueReceive(sniff_queue, &msg, portMAX_DELAY) == pdTRUE) {
            send_to_server(msg.mac, msg.rssi);
        }
    }
}

static void wifi_sniffer_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
    if (type != WIFI_PKT_MGMT && type != WIFI_PKT_DATA) {
        return;
    }

    wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t *)buf;
    const uint8_t *payload = pkt->payload;
    const uint8_t *mac = payload + 10;
    int rssi = pkt->rx_ctrl.rssi;

    if ((mac[0] == 0xFF && mac[1] == 0xFF && mac[2] == 0xFF &&
         mac[3] == 0xFF && mac[4] == 0xFF && mac[5] == 0xFF) ||
        (mac[0] == 0x00 && mac[1] == 0x00 && mac[2] == 0x00 &&
         mac[3] == 0x00 && mac[4] == 0x00 && mac[5] == 0x00)) {
        return;
    }

    static TickType_t last_tick = 0;
    TickType_t now = xTaskGetTickCountFromISR();

    if ((now - last_tick) < pdMS_TO_TICKS(1000)) {
        return;
    }
    last_tick = now;

    sniff_msg_t msg;
    snprintf(msg.mac, sizeof(msg.mac),
             "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    msg.rssi = rssi;

    BaseType_t hp_task_woken = pdFALSE;
    xQueueSendFromISR(sniff_queue, &msg, &hp_task_woken);

    if (hp_task_woken) {
        portYIELD_FROM_ISR();
    }
}

static void enable_sniffer(void) {
    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT |
                       WIFI_PROMIS_FILTER_MASK_DATA
    };

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(false));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(&wifi_sniffer_cb));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    ESP_LOGI(TAG, "Sniffer ON");
}

void app_main(void) {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    sniff_queue = xQueueCreate(20, sizeof(sniff_msg_t));
    if (!sniff_queue) {
        ESP_LOGE(TAG, "Failed to create queue");
        return;
    }

    wifi_init_sta();

    while (!got_ip) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }

    xTaskCreate(sender_task, "sender_task", 4096, NULL, 5, NULL);
    enable_sniffer();
}