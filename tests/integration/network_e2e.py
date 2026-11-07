"""Verify managed installation, authentication and reconnection on one Docker network."""

import asyncio
import json

from connection_e2e import Connection
from live_e2e import NAME, ROOT, until
from managed_e2e import private_command


class Network(Connection):
    async def managed(self, page, frame):
        setup = self.runtime.parent / "setup.json"
        state = json.loads(setup.read_text())
        state.update({name: self.settings[name] for name in ("api_port", "management_port")})
        setup.write_text(json.dumps(state))
        await page.reload()
        await frame.locator("#startup").wait_for(state="hidden", timeout=30000)
        await frame.locator("#tab-setup").click()
        await frame.locator("#prepare").click()
        await until(lambda: frame.locator("#install-command").input_value(), timeout=20)
        private_command("managed_install", await frame.locator("#install-command").input_value())
        await self.control("managed_install")
        self.settings = json.loads((ROOT / "live-settings.json").read_text())
        await frame.locator('[data-close="deployment-dialog"]').click()
        bootstrap = await self.bootstrap()
        target = {"mode": "managed", "installation_id": bootstrap["installation_id"]}
        await self.http("POST", self.prefix + "/managed/connect", payload={
            **target, "config_revision": bootstrap["config_revision"], "request_id": "network-connect",
            **{kind + "_provider_id": "delivery-" + kind for kind in ("chat", "embedding", "rerank")},
        })

        async def connected():
            job = await self.http("POST", self.prefix + "/managed/status", payload=target)
            if job.get("status") == "failed":
                raise RuntimeError(job["message"])
            return job if job.get("status") == "succeeded" else None

        await until(connected, timeout=360)
        await self.usable()
        before = await self.config()
        self.passed("generated_installer_connects_real_astrbot_and_hindsight_on_" + self.settings["network_mode"])
        api_url = self.settings["hindsight_url"]
        management_url = api_url.rsplit(":", 1)[0] + ":8766"
        async with self.session.get(management_url + "/status") as response:
            assert response.status == 401
        async with self.session.get(api_url + "/v1/default/banks") as response:
            assert response.status == 401
        self.report["host_bindings"] = self.settings["verified_host_bindings"]
        self.passed("api_and_management_reject_missing_credentials_and_host_bindings_match_network")

        bootstrap = await self.bootstrap()
        result = await self.http("POST", self.prefix + "/install/prepare", payload={
            **target, "config_revision": bootstrap["config_revision"],
        })
        private_command("managed_reinstall", result["command"])
        await self.control("managed_reinstall")
        await self.http("POST", "/api/v1/plugins/" + NAME + "/reload", payload={})
        await self.usable()
        after = await self.config()
        for field in ("api_url", "api_key", "service_id"):
            assert after[field] == before[field], field
        self.passed("redeployment_and_plugin_reload_preserve_credentials_and_service_identity")
        result = await self.http("POST", self.prefix + "/uninstall/prepare", payload={"delete_memory_data": True})
        private_command("managed_uninstall", result["command"])
        await self.control("managed_uninstall")
        self.passed("uninstall_preserves_astrbot_and_its_network")


async def main():
    acceptance = Network()
    try:
        await acceptance.run()
    except Exception as error:
        acceptance.report.update(failure_type=type(error).__name__, failure=str(error)[:300])
        acceptance.save()
        raise


if __name__ == "__main__":
    asyncio.run(main())
