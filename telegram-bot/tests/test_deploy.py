import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy
import lambda_function as bot


class DeployTests(unittest.TestCase):
    def test_template_contains_source_and_valid_public_function_url_permissions(self):
        template = deploy.build_template()
        function = template["Resources"]["Bot"]["Properties"]
        compile(function["Code"]["ZipFile"], "index.py", "exec")
        self.assertLess(len(json.dumps(template, ensure_ascii=False).encode()),51200)
        self.assertTrue(template["Parameters"]["BotToken"]["NoEcho"])
        self.assertTrue(template["Parameters"]["WebhookSecret"]["NoEcho"])
        self.assertEqual(template["Resources"]["AllowFunctionUrl"]["Properties"]["Action"],"lambda:InvokeFunctionUrl")
        self.assertTrue(template["Resources"]["AllowInvokeViaUrl"]["Properties"]["InvokedViaFunctionUrl"])
        self.assertEqual(template["Resources"]["ConsumeUpdates"]["Properties"]["BatchSize"],1)
        self.assertIn("ReportBatchItemFailures",template["Resources"]["ConsumeUpdates"]["Properties"]["FunctionResponseTypes"])
        self.assertGreaterEqual(template["Resources"]["Updates"]["Properties"]["VisibilityTimeout"],function["Timeout"]*6)
        self.assertTrue(template["Resources"]["Updates"]["Properties"]["FifoQueue"])

    def test_empty_defaults_and_example(self):
        self.assertEqual(bot.BOT_TOKEN, "")
        self.assertEqual(bot.WEBHOOK_SECRET, "")
        self.assertEqual(bot.RECIPIENT_CHAT_ID, "")
        example=json.loads((deploy.ROOT/"settings.example.json").read_text())
        self.assertEqual(example["bot_token"],"")
        self.assertEqual(example["webhook_secret"],"")
        self.assertEqual(example["recipient_username"],"maa_ri888")

    def test_zip_only_contains_the_runtime_code(self):
        with tempfile.TemporaryDirectory() as folder:
            deploy.build_artifacts(Path(folder))
            with zipfile.ZipFile(Path(folder)/"bot.zip") as archive:
                self.assertEqual(archive.namelist(),["lambda_function.py"])
                compile(archive.read("lambda_function.py"),"lambda_function.py","exec")

    def test_secrets_are_preserved_for_redeploy_and_not_logged_or_sent_in_cli_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            config={"region":"eu-central-1","stack_name":"test-visamb","webhook_secret":""}
            state=Path(folder)/"state.json"
            secret=deploy.webhook_secret(config,state)
            self.assertEqual(secret,deploy.webhook_secret(config,state))
            self.assertGreaterEqual(len(secret),16)
            self.assertEqual(state.stat().st_mode & 0o777,0o600)
            self.assertNotIn("bot_token",state.read_text())

    def test_parameters_are_private_files_and_updates_can_be_repeated(self):
        config={"region":"eu-central-1","stack_name":"test-visamb","bot_token":"12345:TEST_ONLY_TOKEN",
                "webhook_secret":"test_webhook_secret_123","recipient_username":"maa_ri888","recipient_chat_id":"","data_retention_days":30}
        captured=[]

        def fake_aws(region,*args,allow_error=False):
            captured.append(args)
            if args[:2]==("cloudformation","describe-stacks"):
                outputs=[{"OutputKey":"WebhookUrl","OutputValue":"https://test.lambda-url.eu-central-1.on.aws/"}]
                return subprocess.CompletedProcess(args,0,json.dumps(outputs),"")
            if args[:2]==("cloudformation","update-stack"):
                parameter_file=Path(args[args.index("--parameters")+1].removeprefix("file://"))
                self.assertEqual(parameter_file.stat().st_mode & 0o777,0o600)
                parameters=json.loads(parameter_file.read_text())
                self.assertIn({"ParameterKey":"BotToken","ParameterValue":config["bot_token"]},parameters)
                self.assertNotIn(config["bot_token"]," ".join(args))
                return subprocess.CompletedProcess(args,255,"","No updates are to be performed.")
            raise AssertionError(args)

        with mock.patch.object(deploy,"aws",side_effect=fake_aws):
            outputs=deploy.deploy(config,deploy.build_template())
        self.assertIn("WebhookUrl",outputs)
        self.assertFalse(any("wait" in args for args in captured))

    def test_first_deploy_creates_waits_then_reads_outputs(self):
        config={"region":"eu-central-1","stack_name":"test-visamb","bot_token":"12345:TEST_ONLY_TOKEN",
                "webhook_secret":"test_webhook_secret_123","recipient_username":"maa_ri888","recipient_chat_id":"","data_retention_days":30}
        calls=[]
        def fake_aws(region,*args,allow_error=False):
            calls.append(args)
            if len(calls)==1:return subprocess.CompletedProcess(args,255,"","Stack does not exist")
            if "--query" in args:return subprocess.CompletedProcess(args,0,json.dumps([{"OutputKey":"WebhookUrl","OutputValue":"https://test.lambda-url.eu-central-1.on.aws/"}]),"")
            return subprocess.CompletedProcess(args,0,"{}","")
        with mock.patch.object(deploy,"aws",side_effect=fake_aws),contextlib.redirect_stdout(io.StringIO()):
            deploy.deploy(config,deploy.build_template())
        self.assertEqual(calls[1][1],"create-stack")
        self.assertIn("stack-create-complete",calls[2])

    def test_one_command_configures_webhook_without_discarding_pending_customers(self):
        fake_config={"region":"eu-central-1","stack_name":"test-visamb","bot_token":"12345:TEST_ONLY_TOKEN",
                     "webhook_secret":"","recipient_username":"maa_ri888","recipient_chat_id":"","data_retention_days":30}
        url="https://example.lambda-url.eu-central-1.on.aws/"
        def telegram(token,method,args):
            if method=="getMe":return {"is_bot":True,"username":"VisaMB_test_bot"}
            if method=="getWebhookInfo":return {"url":url}
            return True
        output=io.StringIO()
        with mock.patch.object(deploy.shutil,"which",return_value="/aws"), \
             mock.patch.object(deploy,"load_config",return_value=fake_config), \
             mock.patch.object(deploy,"webhook_secret",return_value="test_webhook_secret_123"), \
             mock.patch.object(deploy,"deploy",return_value={"WebhookUrl":url}), \
             mock.patch.object(deploy,"telegram_call",side_effect=telegram) as api, \
             contextlib.redirect_stdout(output):
            deploy.main([])
        webhook=next(call.args[2] for call in api.call_args_list if call.args[1]=="setWebhook")
        self.assertFalse(webhook["drop_pending_updates"])
        self.assertEqual(webhook["max_connections"],1)
        self.assertEqual(webhook["secret_token"],"test_webhook_secret_123")
        self.assertIn("https://t.me/VisaMB_test_bot",output.getvalue())
        self.assertNotIn(fake_config["bot_token"],output.getvalue())
        self.assertNotIn(webhook["secret_token"],output.getvalue())

    def test_config_validation_accepts_easy_recipient_change(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch.dict(os.environ,{},clear=True):
            settings=Path(folder)/"settings.json"
            settings.write_text(json.dumps({"recipient_username":"@New_Recipient"}))
            self.assertEqual(deploy.load_config(settings)["recipient_username"],"new_recipient")
            settings.write_text(json.dumps({"recipient_chat_id":1001}))
            with self.assertRaises(deploy.DeployError):deploy.load_config(settings)


if __name__=="__main__":
    unittest.main()
