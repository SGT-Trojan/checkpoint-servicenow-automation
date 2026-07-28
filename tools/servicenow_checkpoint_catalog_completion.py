#!/usr/bin/env python3
"""Install the Check Point CHG-to-RITM/REQ completion business rule."""
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.parse
import urllib.request
from typing import Any


RULE_NAME = "CP FW - complete catalog chain"
TASK_RULE_NAME = "CP FW - retire default catalog tasks"
RULE_SCRIPT = r"""(function executeRule(current, previous) {
    var marker = '[CHECKPOINT_AUTOMATION]';
    var description = current.description ? current.description.toString() : '';
    var state = current.state ? current.state.toString() : '';
    if (description.indexOf(marker) < 0 || (state != '3' && state != '4') || !current.parent)
        return;

    var ritm = new GlideRecord('sc_req_item');
    if (!ritm.get(current.parent.toString()))
        return;

    var itemName = ritm.cat_item ? ritm.cat_item.getDisplayValue() : '';
    if (itemName != 'CheckPoint FW Maintenance Activity')
        return;

    var successful = state == '3' && current.close_code && current.close_code.toString() == 'successful';
    var finalState = successful ? '3' : '4';
    var finalStage = successful ? 'complete' : 'closed_incomplete';
    var outcome = successful ? 'completed successfully' : 'closed without a successful change outcome';
    var note = 'Governed Check Point change ' + current.number + ' ' + outcome + '. Catalog fulfillment reconciled automatically.';

    // Close the RITM before its DEFAULT delivery tasks. The legacy delivery
    // engine can synchronously open the next task when the prior task closes;
    // an inactive RITM lets the companion before-rule reject that reopen.
    ritm.state = finalState;
    ritm.stage = finalStage;
    ritm.active = false;
    ritm.close_notes = note;
    ritm.update();

    var deliveryTask = new GlideRecord('sc_task');
    deliveryTask.addQuery('request_item', ritm.sys_id);
    deliveryTask.addQuery('active', true);
    var taskNames = deliveryTask.addQuery('short_description', 'Assess or Scope Task');
    taskNames.addOrCondition('short_description', 'Provide requested service');
    deliveryTask.query();
    while (deliveryTask.next()) {
        deliveryTask.state = finalState;
        deliveryTask.active = false;
        deliveryTask.close_notes = note + ' This was a global DEFAULT delivery-plan task and required no operator action.';
        deliveryTask.update();
    }

    if (!ritm.request)
        return;
    var siblings = new GlideRecord('sc_req_item');
    siblings.addQuery('request', ritm.request.toString());
    siblings.addQuery('active', true);
    siblings.setLimit(1);
    siblings.query();
    if (siblings.hasNext())
        return;

    var request = new GlideRecord('sc_request');
    if (!request.get(ritm.request.toString()))
        return;
    request.state = finalState;
    request.request_state = successful ? 'closed_complete' : 'closed_incomplete';
    request.stage = successful ? 'closed_complete' : 'closed_incomplete';
    request.active = false;
    request.close_notes = note;
    request.update();
})(current, previous);"""

TASK_RULE_SCRIPT = r"""(function executeRule(current, previous) {
    var shortDescription = current.short_description ? current.short_description.toString() : '';
    if (shortDescription != 'Assess or Scope Task' && shortDescription != 'Provide requested service')
        return;
    if (!current.request_item)
        return;

    var ritm = new GlideRecord('sc_req_item');
    if (!ritm.get(current.request_item.toString()) || ritm.active)
        return;
    var itemName = ritm.cat_item ? ritm.cat_item.getDisplayValue() : '';
    if (itemName != 'CheckPoint FW Maintenance Activity')
        return;

    var chg = new GlideRecord('change_request');
    chg.addQuery('parent', ritm.sys_id);
    chg.addQuery('description', 'CONTAINS', '[CHECKPOINT_AUTOMATION]');
    chg.addQuery('state', 'IN', '3,4');
    chg.orderByDesc('sys_updated_on');
    chg.setLimit(1);
    chg.query();
    if (!chg.next())
        return;

    var successful = chg.state.toString() == '3' && chg.close_code && chg.close_code.toString() == 'successful';
    current.state = successful ? '3' : '4';
    current.active = false;
    current.close_notes = 'Governed Check Point change ' + chg.number +
        (successful ? ' completed successfully.' : ' closed without a successful outcome.') +
        ' This global DEFAULT delivery-plan task is automation-managed and requires no operator action.';
})(current, previous);"""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"ERROR: {name} is required")
    return value


class Api:
    def __init__(self) -> None:
        self.base = required_env("SN_INSTANCE").rstrip("/")
        auth = base64.b64encode(
            f"{required_env('SN_USERNAME')}:{required_env('SN_PASSWORD')}".encode()
        ).decode()
        self.headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def call(
        self,
        method: str,
        table_path: str,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        request = urllib.request.Request(
            f"{self.base}/api/now/table/{table_path}{suffix}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        for key, value in self.headers.items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode())

    def results(self, table: str, query: str, fields: str) -> list[dict[str, Any]]:
        return self.call(
            "GET",
            table,
            {
                "sysparm_query": query,
                "sysparm_fields": fields,
                "sysparm_limit": "10",
            },
        ).get("result", [])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    api = Api()
    definitions = [
        (RULE_NAME, "change_request", "after", "descriptionLIKE[CHECKPOINT_AUTOMATION]^stateIN3,4", RULE_SCRIPT),
        (TASK_RULE_NAME, "sc_task", "before", "short_descriptionINAssess or Scope Task,Provide requested service", TASK_RULE_SCRIPT),
    ]
    records = {}
    for name, _, _, _, script in definitions:
        existing = api.results("sys_script", f"name={name}", "sys_id,name,active,script")
        records[name] = existing
        changed = not existing or existing[0].get("script") != script or existing[0].get("active") != "true"
        print(f"{name}: present={bool(existing)} changed={changed}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to create or update the rules.")
        return 0
    for name, collection, when, condition, script in definitions:
        body = {
            "name": name,
            "collection": collection,
            "when": when,
            "order": "900",
            "active": "true",
            "action_update": "true",
            "action_insert": "true" if collection == "sc_task" else "false",
            "action_delete": "false",
            "filter_condition": condition,
            "script": script,
            "description": "Reconciles governed Check Point catalog fulfillment without affecting other catalog items.",
        }
        existing = records[name]
        if existing:
            api.call("PATCH", f"sys_script/{existing[0]['sys_id']}", body=body)
            print(f"Updated {name}")
        else:
            result = api.call("POST", "sys_script", body=body)["result"]
            print(f"Created {name}: {result['sys_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
