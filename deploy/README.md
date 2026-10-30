# Voxonta для Matrix

Самостоятельный установочный комплект: интеграция с виджетом, захват LiveKit,
Postgres и модуль Synapse. Nextcloud, его приложение, Talk и видеорекордер
для работы не требуются. Устанавливается в существующую систему Synapse +
Element Call + LiveKit и подключается к вашей учётке Voxonta.

1. Скопируйте `.env.example` в `.env`, заполните адреса и ключи. Для новых секретов
   используйте независимые случайные hex-строки длиной минимум 32 символа.
2. Установите включённый `voxonta_widget_adapter.py` на существующий Synapse.
   Он проверен на версии 1.163.2. Доступ к endpoint адаптера разрешите только
   интеграции; bearer key должен совпадать с WIDGET_ADAPTER_TOKEN.
3. Настройте внешний HTTPS proxy `/widget/` на `127.0.0.1:8082` без удаления
   префикса. Разрешённый origin Element задаётся в WIDGET_PARENT_ORIGINS.
4. Выполните `docker compose up -d --build` из каталога комплекта.
5. Установите отдельный виджет Voxonta в тестовую комнату. Автоматическая
   установка опциональна и не включает транскрибацию. Стартует администратор.

Пример конфигурации Synapse (не секреты из примера, а собственные значения):

```yaml
modules:
  - module: voxonta_widget_adapter.VoxontaWidgetAdapter
    config:
      token: <WIDGET_ADAPTER_TOKEN>
      widget_key: voxonta
      widget_url: https://voxonta.example.org/widget/?roomId=$matrix_room_id
      auto_install: false
```

State `im.vector.modular.widgets`, key `voxonta`:

```json
{"id":"voxonta","type":"m.custom","name":"Voxonta","waitForIframeLoad":true,
 "url":"https://voxonta.example.org/widget/?roomId=$matrix_room_id"}
```

Секреты остаются в вашей `.env` и конфигурации Synapse. Служебные порты захвата
и база наружу не публикуются. Контейнеры integration/capture — внутренние части
одного комплекта, а не отдельные продукты, которые нужно искать и устанавливать.
Проверка: `docker compose ps`, `docker compose logs --tail=100 integration capture`.
Остановка: `docker compose down` (без `-v`, чтобы сохранить архив и настройки).

Полная инструкция по правам, миграции с бота, приёмке Element и откату находится
в `integration/docs/widget.md`. Базу нужно резервировать, включая виджетный архив.
Следуйте проверке Element Web/Desktop перед боевым включением. Выпуск комплекта
не устанавливает и не перезапускает ваш Synapse автоматически.
