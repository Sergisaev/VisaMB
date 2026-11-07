import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lambda_function as bot

OWNER = {"id": 1001, "first_name": "Сергей", "username": "sergikos9"}
CLIENT = {"id": 2002, "first_name": "Мария", "last_name": "Тестовая", "username": "maria_client"}


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return copy.deepcopy(self.items.get(key))

    def commit(self, update_id, changes, operations):
        key = "update:" + str(update_id)
        if key not in self.items:
            self.items.update(copy.deepcopy(changes))
            self.items[key] = copy.deepcopy({"operations": operations, "cursor": 0, "done": not operations})
        return self.get(key)

    def advance(self, update_id, record, result, operation, recipient_id=None):
        updated = {**record, "cursor": record["cursor"] + 1}
        updated["done"] = updated["cursor"] == len(updated["operations"])
        self.items["update:" + str(update_id)] = copy.deepcopy(updated)
        if operation.get("application_id"):
            self.items["application:" + operation["application_id"]].update(
                status="sent", recipient_chat_id=recipient_id, telegram_message_id=result.get("message_id"))
        return updated


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail_once = None

    def call(self, method, arguments):
        if self.fail_once and self.fail_once(method, arguments):
            self.fail_once = None
            raise bot.TelegramError("Simulated delivery failure")
        self.sent.append((method, copy.deepcopy(arguments)))
        return {"message_id": len(self.sent)}

    def messages(self, chat_id):
        return [args for method, args in self.sent if method == "sendMessage" and args["chat_id"] == chat_id]


class Flow:
    def __init__(self, store=None, settings=None):
        self.store = store or MemoryStore()
        self.telegram = FakeTelegram()
        self.settings = settings or bot.Settings("12345:TEST_ONLY_TOKEN", "test_webhook_secret_123", "sergikos9", table_name="test", queue_url="test")
        self.queue = mock.Mock()
        self.runtime = bot.Runtime(self.settings, self.store, self.telegram, self.queue)
        self.update_id = 0

    def message(self, text, user=CLIENT, **extra):
        self.update_id += 1
        update = {"update_id": self.update_id, "message": {
            "from": user, "chat": {"id": user["id"], "type": "private"}, "text": text, **extra}}
        self.runtime.process(update)
        return update

    def callback_update(self, action, user=CLIENT, nonce=None):
        self.update_id += 1
        session = self.session(user)
        return {"update_id": self.update_id, "callback_query": {
            "id": "callback-" + str(self.update_id), "from": user,
            "data": action + ":" + (nonce or session["id"]),
            "message": {"chat": {"id": user["id"], "type": "private"}}}}

    def click(self, action, user=CLIENT, **kwargs):
        update = self.callback_update(action, user, **kwargs)
        self.runtime.process(update)
        return update

    def session(self, user=CLIENT):
        return self.store.get("session:" + str(user["id"]))

    def complete(self, user=CLIENT):
        self.message("/start", user)
        self.message("Алина", user)
        self.click("pick:destination:japan", user)
        self.message("РФ, проживаю в России", user)
        self.click("pick:purpose:tourism", user)
        self.message("В апреле, на 10 дней", user)
        self.click("pick:history:none", user)
        self.click("skip", user)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.flow = Flow()
        self.flow.message("/start", OWNER)

    def test_full_intake_sends_identity_and_all_answers_only_after_confirmation(self):
        f = self.flow
        owner_count = len(f.telegram.messages(OWNER["id"]))
        f.complete()
        self.assertEqual(f.session()["mode"], "review")
        self.assertEqual(len(f.telegram.messages(OWNER["id"])), owner_count)
        self.assertIn("@maria_client", f.telegram.messages(CLIENT["id"])[-1]["text"])
        update = f.click("submit")
        notification = f.telegram.messages(OWNER["id"])[-1]
        for value in ("Алина", "Япония", "РФ, проживаю в России", "Туризм", "на 10 дней", "Виз не было", "@maria_client", "2002"):
            self.assertIn(value, notification["text"])
        self.assertEqual(notification["reply_markup"]["inline_keyboard"][0][0]["url"], "https://t.me/maria_client")
        self.assertIn("отправлена!", f.telegram.messages(CLIENT["id"])[-1]["text"])
        app = f.store.get("application:" + f.session()["id"])
        self.assertEqual(app["status"], "sent")
        self.assertEqual(app["recipient_chat_id"], OWNER["id"])
        count = len(f.telegram.sent)
        f.runtime.process(update)
        self.assertEqual(len(f.telegram.sent), count)
        f.click("submit")
        self.assertEqual(len(f.telegram.messages(OWNER["id"])), owner_count + 1)
        self.assertFalse(any("привяз" in args["text"].lower() for args in f.telegram.messages(CLIENT["id"])))

    def test_without_username_asks_for_contact_without_account_linking(self):
        user = {key: value for key, value in CLIENT.items() if key != "username"}
        f = self.flow
        f.complete(user)
        self.assertEqual(f.session(user)["field"], "contact")
        f.message("не контакт", user)
        self.assertEqual(f.session(user)["field"], "contact")
        f.message("client@example.com", user)
        self.assertEqual(f.session(user)["mode"], "review")
        f.click("submit", user)
        notification = f.telegram.messages(OWNER["id"])[-1]
        self.assertIn("ник не указан", notification["text"])
        self.assertIn("client@example.com", notification["text"])
        self.assertIn("2002", notification["text"])
        self.assertEqual(notification["reply_markup"]["inline_keyboard"][0][0]["url"], "tg://user?id=2002")

    def test_edit_answers_then_return_to_review(self):
        f = self.flow
        f.complete()
        f.click("edit")
        f.click("editfield:dates")
        f.message("В мае, даты гибкие")
        self.assertEqual(f.session()["mode"], "review")
        self.assertEqual(f.session()["answers"]["dates"], "В мае, даты гибкие")
        self.assertEqual(f.session()["answers"]["destination"], "Япония")

    def test_back_and_cancel_do_not_send_application(self):
        f = self.flow
        count = len(f.telegram.messages(OWNER["id"]))
        f.message("/start")
        f.message("Алина")
        f.click("back")
        self.assertEqual(f.session()["field"], "name")
        f.message("Аня")
        f.click("cancel")
        self.assertEqual(f.session()["mode"], "cancelled")
        self.assertEqual(len(f.telegram.messages(OWNER["id"])), count)

    def test_old_buttons_cannot_modify_new_session(self):
        f = self.flow
        f.message("/start")
        nonce = f.session()["id"]
        f.message("/start")
        f.click("pick:name:use", nonce=nonce)
        self.assertEqual(f.session()["answers"], {})
        self.assertNotEqual(f.session()["id"], nonce)

    def test_invalid_country_and_overlong_answers_do_not_advance(self):
        f = self.flow
        f.message("/start")
        f.message("X" * 81)
        self.assertEqual(f.session()["field"], "name")
        f.click("pick:name:use")
        f.message("Италия")
        self.assertEqual(f.session()["field"], "destination")
        f.click("pick:destination:schengen")
        self.assertEqual(f.session()["answers"]["destination"], "Шенген")

    def test_media_does_not_silently_skip_optional_question(self):
        f = self.flow
        f.complete()
        f.click("editfield:comment")
        f.message("", photo=[{"file_id": "not-downloaded"}])
        self.assertEqual(f.session()["field"], "comment")
        self.assertEqual(f.session()["mode"], "question")

    def test_username_is_refreshed_before_sending_and_input_is_plain_text(self):
        f = self.flow
        f.complete()
        f.click("editfield:comment")
        f.message("<b>Мой вопрос & детали</b>")
        renamed = {**CLIENT, "username": "new_client_name"}
        f.click("submit", renamed)
        notification = f.telegram.messages(OWNER["id"])[-1]
        self.assertIn("@new_client_name", notification["text"])
        self.assertIn("<b>Мой вопрос & детали</b>", notification["text"])
        self.assertNotIn("parse_mode", notification)

    def test_owner_username_change_keeps_numeric_binding(self):
        f = self.flow
        renamed = {**OWNER, "username": "changed_owner"}
        f.message("/start", renamed)
        self.assertIn("Получение заявок включено", f.telegram.messages(OWNER["id"])[-1]["text"])
        f.complete()
        f.click("submit")
        self.assertIn("ЗАЯВКА", f.telegram.messages(OWNER["id"])[-1]["text"])

    def test_recipient_is_conveniently_changeable(self):
        f = self.flow
        new_owner = {"id": 3003, "first_name": "Новый получатель", "username": "new_recipient"}
        f.settings = bot.Settings("12345:TEST_ONLY_TOKEN", "test_webhook_secret_123", "new_recipient")
        f.runtime.settings = f.settings
        f.message("/start", new_owner)
        previous_count = len(f.telegram.messages(OWNER["id"]))
        f.complete()
        f.click("submit")
        self.assertEqual(len(f.telegram.messages(OWNER["id"])), previous_count)
        self.assertIn("ЗАЯВКА", f.telegram.messages(new_owner["id"])[-1]["text"])

    def test_failure_after_notification_does_not_send_the_lead_twice(self):
        f = self.flow
        f.complete()
        update = f.callback_update("submit")
        f.telegram.fail_once = lambda method, args: method == "sendMessage" and "отправлена!" in args["text"]
        with self.assertRaises(bot.TelegramError):
            f.runtime.process(update)
        notifications = len(f.telegram.messages(OWNER["id"]))
        self.assertEqual(f.store.get("application:" + f.session()["id"])["status"], "sent")
        f.runtime.process(update)
        self.assertEqual(len(f.telegram.messages(OWNER["id"])), notifications)
        self.assertIn("отправлена!", f.telegram.messages(CLIENT["id"])[-1]["text"])

    def test_failed_delivery_is_saved_and_never_confirmed_as_sent(self):
        f = self.flow
        f.complete()
        update = f.callback_update("submit")
        f.telegram.fail_once = lambda method, args: method == "sendMessage" and args["chat_id"] == OWNER["id"]
        with self.assertRaises(bot.TelegramError):
            f.runtime.process(update)
        application = f.store.get("application:" + f.session()["id"])
        self.assertEqual(application["status"], "pending")
        self.assertNotIn("отправлена!", f.telegram.messages(CLIENT["id"])[-1]["text"])
        f.runtime.process(update)
        self.assertEqual(f.store.get("application:" + f.session()["id"])["status"], "sent")

    def test_expired_callback_ack_does_not_block_application(self):
        f = self.flow
        f.complete()
        f.telegram.fail_once = lambda method, args: method == "answerCallbackQuery"
        f.click("submit")
        self.assertEqual(f.store.get("application:" + f.session()["id"])["status"], "sent")

    def test_concurrent_clients_keep_separate_answers(self):
        f = self.flow
        second = {"id": 4004, "first_name": "Другой клиент", "username": "other_client"}
        f.message("/start")
        f.message("/start", second)
        f.message("Имя первого")
        f.message("Имя второго", second)
        self.assertNotEqual(f.session()["id"], f.session(second)["id"])
        self.assertEqual(f.session()["answers"]["name"], "Имя первого")
        self.assertEqual(f.session(second)["answers"]["name"], "Имя второго")

    def test_long_emoji_text_is_preserved_and_split_at_telegram_limit(self):
        value = "🌍" * 4000 + "\n&<end>"
        parts = bot.chunks(value)
        self.assertEqual("".join(parts), value)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts))

    def test_unregistered_recipient_blocks_intake_and_cannot_be_hijacked(self):
        f = Flow()
        f.message("/start")
        self.assertIsNone(f.session())
        self.assertIsNone(f.store.get("recipient:sergikos9"))
        self.assertIn("готовится", f.telegram.messages(CLIENT["id"])[-1]["text"])

    def test_explicit_recipient_id_routes_without_username_registration(self):
        settings = bot.Settings("12345:TEST_ONLY_TOKEN", "test_webhook_secret_123", recipient_chat_id="1001")
        f = Flow(settings=settings)
        f.complete()
        f.click("submit")
        self.assertIn("ЗАЯВКА", f.telegram.messages(OWNER["id"])[-1]["text"])

    def test_groups_are_ignored(self):
        f = self.flow
        before = len(f.telegram.sent)
        f.runtime.process({"update_id": 9999, "message": {"chat": {"id": -100, "type": "group"}, "from": CLIENT, "text": "/start"}})
        self.assertEqual(len(f.telegram.sent), before)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.flow = Flow()
        self.environment = mock.patch.dict(bot.os.environ, {"BOT_TOKEN":"12345:TEST_ONLY_TOKEN",
            "WEBHOOK_SECRET":"test_webhook_secret_123", "STATE_TABLE":"test-table", "UPDATE_QUEUE_URL":"test-queue"})
        self.environment.start()
        self.runtime = mock.patch.object(bot, "get_runtime", return_value=self.flow.runtime)
        self.runtime.start()

    def tearDown(self):
        self.runtime.stop()
        self.environment.stop()

    def event(self, update=None, secret="test_webhook_secret_123"):
        return {"requestContext":{"http":{"method":"POST"}},
                "headers":{"X-Telegram-Bot-Api-Secret-Token":secret},
                "body":json.dumps(update or {"update_id":10,"message":{"chat":{"id":2002,"type":"private"}}})}

    def test_secret_is_checked_before_queue_or_state_access(self):
        for secret in ("", "wrong", "☃"):
            with self.subTest(secret=secret):
                result = bot.lambda_handler(self.event(secret=secret), None)
                self.assertEqual(result["statusCode"], 403)
        self.flow.queue.send_message.assert_not_called()

    def test_valid_update_is_enqueued_by_chat_and_deduplicated_by_update_id(self):
        event = self.event()
        event.update(isBase64Encoded=True, body=base64.b64encode(event["body"].encode()).decode())
        self.assertEqual(bot.lambda_handler(event, None)["statusCode"], 200)
        arguments = self.flow.queue.send_message.call_args.kwargs
        self.assertEqual(arguments["MessageGroupId"], "2002")
        self.assertEqual(arguments["MessageDeduplicationId"], "10")

    def test_queue_failure_returns_retryable_status(self):
        self.flow.queue.send_message.side_effect = RuntimeError("simulated")
        self.assertEqual(bot.lambda_handler(self.event(), None)["statusCode"], 503)

    def test_invalid_and_oversized_payloads_are_rejected(self):
        for body, expected in (("[]",400),("not JSON",400),("{" + "x"*90001,413)):
            event = self.event()
            event["body"] = body
            self.assertEqual(bot.lambda_handler(event,None)["statusCode"], expected)

    def test_empty_secrets_fail_closed(self):
        with mock.patch.dict(bot.os.environ, {"BOT_TOKEN":"", "WEBHOOK_SECRET":""}):
            self.assertEqual(bot.lambda_handler(self.event(),None)["statusCode"],503)

    def test_sqs_failure_keeps_remaining_fifo_records_for_retry(self):
        self.flow.runtime.process = mock.Mock(side_effect=bot.TelegramError("simulated"))
        event = {"Records":[{"eventSource":"aws:sqs","messageId":str(i),"body":json.dumps({"update_id":i})} for i in (1,2)]}
        result=bot.lambda_handler(event,None)
        self.assertEqual(result,{"batchItemFailures":[{"itemIdentifier":"1"},{"itemIdentifier":"2"}]})
        self.assertEqual(self.flow.runtime.process.call_count,1)

    def test_http_payload_cannot_impersonate_an_sqs_invocation(self):
        event=self.event({"update_id":10,"Records":[{"messageId":"spoofed"}]})
        self.assertEqual(bot.lambda_handler(event,None)["statusCode"],200)
        self.flow.queue.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
