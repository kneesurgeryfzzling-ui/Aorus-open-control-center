"""Faux service gbwmi.ps1 pour les tests : simule le firmware et enregistre les appels."""
import json, os, sys
LOG = os.environ.get("FAKE_LOG", "/tmp/fake_fw.log")
KNOWN = {"SetAutoFanStatus","SetFixedFanStatus","SetStepFanStatus","SetFixedFanSpeed","SetGPUFanDuty","SetFanAdjustStatus",
         "SetNvThermalTarget","SetChargePolicy","SetChargeStop","SetDynamicBoostStatus","SetAIBoostStatus","GetFanIndexValue",
         "GetChargePolicy","GetChargeStop","GetDynamicBoostStatus"}
def out(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
def rec(x):
    with open(LOG,"a") as f: f.write(json.dumps(x)+"\n")
out({"ready": True})
for line in sys.stdin:
    r = json.loads(line); op, i = r["op"], r["id"]
    if op != "sensors": rec({k: v for k, v in r.items() if k != "id"})
    if op == "ping": out({"id":i,"ok":True,"data":"pong"})
    elif op == "info": out({"id":i,"ok":True,"data":{"ps":"5.1","model":"AORUS 15G YC (simulé)","classes":{"GB_WMIACPI_Set":True,"GB_WMIACPI_Get":True,"RW_GMWMI":True},"methods":sorted(KNOWN),"getmethods":["GetRpm1()","GetRpm2()","GetCpuTemp()","GetFanIndexValue(Index:UInt8)"]}})
    elif op == "sensors":
        import math, time; k = time.time()
        ct = 58 + 14 * math.sin(k / 9) + 4 * math.sin(k * 1.3); gt = 54 + 10 * math.sin(k / 13 + 1)
        if os.environ.get("FAKE_NOLHM"): out({"id":i,"ok":True,"data":{"lhm":[],"acpi":[]}}); continue
        out({"id":i,"ok":True,"data":{"lhm":[{"n":"CPU Package","t":"Temperature","v":round(ct,1),"id":"/intelcpu/0/temperature/0"},{"n":"GPU Core","t":"Temperature","v":round(gt,1),"id":"/gpu-nvidia/0/temperature/0"},{"n":"CPU Fan","t":"Fan","v":int(2400+40*ct-1500),"id":"/f/0"},{"n":"GPU Fan","t":"Fan","v":int(2300+45*gt-1500),"id":"/f/1"}],"acpi":[]}})
    elif op == "call":
        import random as _r
        if r["method"] in ("GetRpm1", "GetRpm2", "GetCpuTemp"):
            out({"id":i,"ok":True,"data":{"Data": _r.randint(2600, 3400) if "Rpm" in r["method"] else _r.randint(55, 70)}}); continue
        d = {"Data": 1, "Temperture": 60, "Value": 120} if r["method"].startswith("Get") else {}
        out({"id":i,"ok":r["method"] in KNOWN,"data":d,"err":"méthode inconnue"})
    elif op == "seq": out({"id":i,"ok":True,"data":[{"m":c["method"],"ok":c["method"] in KNOWN,"err":"inconnue"} for c in r["calls"]]})
    elif op == "kbput":
        assert len(r["bytes"]) == 32; out({"id":i,"ok":True,"data":"ok"})
    elif op == "kbget": out({"id":i,"ok":True,"data":[0]*32})
    elif op == "quit": out({"id":i,"ok":True,"data":"bye"}); break
