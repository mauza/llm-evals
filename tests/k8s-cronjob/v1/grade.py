"""Grader: parse the CronJob manifest out of the reply and verify every requirement."""
import re
import sys

import yaml

text = open(sys.argv[1]).read()
m = re.search(r"```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL)
doc = yaml.safe_load(m.group(1) if m else text)

failures = []


def expect(label, actual, want):
    if actual != want:
        failures.append(f"{label}: got {actual!r}, want {want!r}")


expect("kind", doc.get("kind"), "CronJob")
expect("name", doc.get("metadata", {}).get("name"), "media-scrub")
expect("namespace", doc.get("metadata", {}).get("namespace"), "media")

spec = doc.get("spec", {})
if spec.get("schedule") not in ("*/15 * * * *", "0,15,30,45 * * * *"):
    failures.append(f"schedule: got {spec.get('schedule')!r}, want every 15 minutes")
expect("concurrencyPolicy", spec.get("concurrencyPolicy"), "Forbid")
expect("successfulJobsHistoryLimit", spec.get("successfulJobsHistoryLimit"), 3)

pod = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
expect("restartPolicy", pod.get("restartPolicy"), "OnFailure")
containers = pod.get("containers") or [{}]
c = containers[0]
expect("image", c.get("image"), "ghcr.io/mauza/media-scrub:1.4.2")
res = c.get("resources", {})
expect("requests.cpu", str(res.get("requests", {}).get("cpu")), "100m")
expect("requests.memory", str(res.get("requests", {}).get("memory")), "128Mi")
expect("limits.cpu", str(res.get("limits", {}).get("cpu")), "500m")
expect("limits.memory", str(res.get("limits", {}).get("memory")), "512Mi")

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("manifest meets all requirements")
