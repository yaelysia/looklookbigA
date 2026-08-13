import pr_lifecycle_policy as p
from pathlib import Path
H="1"*40; B="2"*40

def body(head=H,close="true"):
    return f"Refs #68\n<!-- looklookbigA-auto-dev-lock -->\n<!-- looklookbigA-auto-state\nSTATE=VALIDATING\nISSUE=68\nPR=69\nCHECKPOINT=READY_FOR_REVIEW_PREPARED\nLAST_HEAD={head}\nISSUE_CLOSE_ON_MERGE={close}\n-->\n"

def pr(draft=False,mergeable=True,base="master"):
    return {"number":69,"state":"open","draft":draft,"mergeable":mergeable,"body":body(),"user":{"login":"yaelysia"},"head":{"sha":H,"repo":{"full_name":"yaelysia/looklookbigA"}},"base":{"ref":base}}

def comment(head=H,base=B):
    return {"user":{"login":"yaelysia"},"body":f"<!-- looklookbigA-auto-review -->\nHEAD_SHA={head}\nBASE_SHA={base}\nVERDICT=PASS_AUTOMERGE\nREASON_CODES=NONE\n"}

def merge(files,**kw):
    return p.auto_merge_decision(pr=kw.pop("candidate",pr()),comment=kw.pop("review",comment()),current_base_sha=B,changed_files=files,exact_gate_valid=kw.pop("exact",True),issue_author="yaelysia",**kw)

def test_exact_binding_and_low_risk():
    run={"name":"Pre-merge Security Gate","path":".github/workflows/pre-merge-security-gate.yml","event":"pull_request","conclusion":"success","head_sha":H}
    assert p.promotion_decision(pr=pr(draft=True),run=run,current_base_sha=B,run_merge_parents=[B,H],changed_files=[],issue_author="yaelysia").allowed
    assert p.verdict_binding_decision(pr=pr(),comment=comment(head="3"*40),current_base_sha=B,issue_author="yaelysia").reason=="STALE_VERDICT_HEAD"
    assert merge(["scripts/example.py"])==p.Decision(True,"MERGE")

def test_high_risk_requires_proof():
    for path in [".github/workflows/x.yml","scripts/pr_lifecycle_policy.py","requirements-dev.txt","Dockerfile"]:
        assert merge([path],high_risk_checks_valid=False).reason=="HIGH_RISK_CHECKS_NOT_VALID"
        assert merge([path],high_risk_checks_valid=True)==p.Decision(True,"MERGE_HIGH_RISK_VERIFIED")
    assert merge(["scripts/example.py"],candidate=pr(base="v1"),high_risk_checks_valid=True).allowed

def test_fail_closed_and_evidence():
    assert merge([],candidate=pr(draft=True)).reason=="PR_IS_DRAFT"
    assert merge([],candidate=pr(mergeable=False)).reason=="PR_NOT_MERGEABLE"
    assert merge([],exact=False).reason=="EXACT_GATE_NOT_VALID"
    assert merge([".automation-locks/x.json"],high_risk_checks_valid=True).reason=="LOCK_ARTIFACT_NOT_REMOVED"
    req=p.high_risk_evidence_requirements([".github/workflows/x.yml"],"master")
    assert {"Safety tests","Reusable smoke","pre-merge-security-gate"}<=req["jobs"]
    assert "Run PR lifecycle policy tests" in req["steps"]

def test_runtime_contract():
    root=Path(__file__).resolve().parents[1]
    wf=(root/".github/workflows/pr-review-merge-gate.yml").read_text()
    rt=(root/"scripts/pr_review_merge_gate.py").read_text()
    assert "python3 scripts/pr_review_merge_gate.py" in wf
    assert "github_pr_lifecycle_gate.py review" not in wf
    assert '"force": False' in rt and "parents != [base_sha, head_sha]" in rt

def test_audit():
    assert p.should_close_issue_on_merge(pr())
    assert not p.should_close_issue_on_merge({**pr(),"body":body(close="false")})
    assert p.merged_parent_decision(B,H,[B,H]).allowed

if __name__=="__main__":
    tests=[test_exact_binding_and_low_risk,test_high_risk_requires_proof,test_fail_closed_and_evidence,test_runtime_contract,test_audit]
    for t in tests:t();print("PASS",t.__name__)
    print("PR_LIFECYCLE_POLICY_TESTS passed=5")
