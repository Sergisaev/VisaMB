"""AWS API integration in Moto, with no requests to a real AWS account."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import lambda_function as bot
from test_bot import Flow,OWNER,CLIENT

try:
    import boto3
    from moto import mock_aws
    import botocore.session
except ImportError:
    boto3=None


@unittest.skipIf(boto3 is None,"Optional AWS test dependencies: requirements-dev.txt")
class AwsStorageTests(unittest.TestCase):
    def setUp(self):
        self.environment=mock.patch.dict(os.environ,{"BOT_TOKEN":"12345:TEST_ONLY_TOKEN",
            "WEBHOOK_SECRET":"test_webhook_secret_123","STATE_TABLE":"test-table","RECIPIENT_USERNAME":"sergikos9"})
        self.environment.start()
        self.aws=mock_aws()
        self.aws.start()
        # Use a separate mock-only SDK profile; preserve the host's identity guards.
        self.directory=tempfile.TemporaryDirectory(prefix="visamb-moto-profile-")
        folder=Path(self.directory.name)
        (folder/"config").write_text("[profile visamb-moto-test]\nregion = us-east-1\n")
        (folder/"credentials").write_text("")
        session=botocore.session.Session(profile="visamb-moto-test")
        session.set_config_variable("config_file",str(folder/"config"))
        session.set_config_variable("credentials_file",str(folder/"credentials"))
        sdk=boto3.Session(botocore_session=session,aws_access_key_id="testing",aws_secret_access_key="testing",aws_session_token="testing",region_name="us-east-1")
        self.metadata_session=mock.patch("moto.core.base_backend.AccountSpecificBackend.get_session",return_value=sdk)
        self.metadata_session.start()
        self.client_patch=mock.patch.object(boto3,"client",side_effect=sdk.client)
        self.client_patch.start()
        self.db=boto3.client("dynamodb",region_name="us-east-1")
        self.db.create_table(TableName="test-table",BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName":"pk","KeyType":"HASH"}],AttributeDefinitions=[{"AttributeName":"pk","AttributeType":"S"}])
        self.settings=bot.Settings("12345:TEST_ONLY_TOKEN","test_webhook_secret_123","sergikos9",table_name="test-table")
        self.store=bot.DynamoStore(self.settings)

    def tearDown(self):
        bot.get_runtime.cache_clear()
        self.client_patch.stop()
        self.metadata_session.stop()
        self.directory.cleanup()
        self.aws.stop()
        self.environment.stop()

    def test_complete_application_survives_a_cold_start_and_duplicate_delivery(self):
        flow=Flow(self.store,self.settings)
        flow.message("/start",OWNER)
        flow.message("/start")
        flow.message("Анна")
        # Replace the store/client as Lambda does when another invocation runs cold.
        flow.store=bot.DynamoStore(self.settings)
        flow.runtime.store=flow.store
        self.assertEqual(flow.session()["answers"]["name"],"Анна")
        flow.click("pick:destination:usa")
        flow.message("РФ, Россия")
        flow.click("pick:purpose:tourism")
        flow.message("Летом")
        flow.message("Шенген, без отказов")
        flow.click("skip")
        update=flow.click("submit")
        sent=len(flow.telegram.messages(OWNER["id"]))
        flow.runtime=bot.Runtime(self.settings,bot.DynamoStore(self.settings),flow.telegram,flow.queue)
        flow.runtime.process(update)
        self.assertEqual(len(flow.telegram.messages(OWNER["id"])),sent)
        application=self.store.get("application:"+flow.session()["id"])
        self.assertEqual(application["status"],"sent")
        self.assertIsInstance(application["telegram_message_id"],int)

    def test_update_transaction_is_atomic_and_outbox_cursor_is_durable(self):
        op=bot.message(2002,"Вопрос")
        record=self.store.commit(100,{"session:2002":{"mode":"question"}},[op])
        duplicate=self.store.commit(100,{"session:2002":{"mode":"wrong"}},[])
        self.assertEqual(duplicate,record)
        self.assertEqual(self.store.get("session:2002"),{"mode":"question"})
        self.store.advance(100,record,{"message_id":77},op)
        self.assertTrue(bot.DynamoStore(self.settings).get("update:100")["done"])
        self.assertEqual(self.store.get("update:100")["cursor"],1)

    def test_owner_binding_does_not_expire_but_client_data_does(self):
        self.store.commit(101,{"recipient:sergikos9":{"chat_id":1001},"session:2002":{"field":"name"}},[])
        owner=self.db.get_item(TableName="test-table",Key={"pk":{"S":"bot:12345:recipient:sergikos9"}})["Item"]
        session=self.db.get_item(TableName="test-table",Key={"pk":{"S":"bot:12345:session:2002"}})["Item"]
        self.assertNotIn("expires_at",owner)
        self.assertIn("expires_at",session)

    def test_fifo_queue_deduplicates_webhook_requests(self):
        queue=boto3.client("sqs",region_name="us-east-1")
        url=queue.create_queue(QueueName="test-updates.fifo",Attributes={"FifoQueue":"true"})["QueueUrl"]
        with mock.patch.dict(os.environ,{"UPDATE_QUEUE_URL":url}):
            event={"requestContext":{"http":{"method":"POST"}},"headers":{"x-telegram-bot-api-secret-token":"test_webhook_secret_123"},
                   "body":json.dumps({"update_id":200,"message":{"chat":{"id":2002,"type":"private"},"from":CLIENT,"text":"/start"}})}
            self.assertEqual(bot.lambda_handler(event,None)["statusCode"],200)
            self.assertEqual(bot.lambda_handler(event,None)["statusCode"],200)
            messages=queue.receive_message(QueueUrl=url,MaxNumberOfMessages=10)["Messages"]
            self.assertEqual(len(messages),1)


if __name__=="__main__":
    unittest.main()
