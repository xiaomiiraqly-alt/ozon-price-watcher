"""Run setup/network scripts with local Git and Docker stand-ins, no live services."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class NetworkScriptTests(unittest.TestCase):
    def test_same_exit_fails_and_distinct_exits_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/"docker"
            binary.write_text(f"#!{sys.executable}\n"+'''
import os,sys
args=sys.argv[1:]
if "awg-client" in args:sys.exit(0)
sys.stdin.read()
print(os.environ["TEST_TG_IP"] if "bot" in args else "203.0.113.2")
''')
            binary.chmod(0o755)
            for ip,code in (("203.0.113.2",1),("198.51.100.1",0)):
                env={**os.environ,"PATH":tmp+os.pathsep+os.environ["PATH"],"TEST_TG_IP":ip}
                result=subprocess.run(["bash",str(ROOT/"scripts/check_network.sh")],env=env,text=True,capture_output=True)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)
                if code:self.assertIn("same exit IP",result.stderr)


class PrepareScriptTests(unittest.TestCase):
    def test_pinned_clone_is_repeatable_and_local_changes_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            upstream=base/"upstream"
            upstream.mkdir()
            git=shutil.which("git")
            def run_git(*args):
                return subprocess.run([git,"-C",str(upstream),*args],check=True,text=True,capture_output=True).stdout.strip()
            run_git("init")
            run_git("config","user.email","ci@example.invalid")
            run_git("config","user.name","Test")
            (upstream/"pyproject.toml").write_text("# pinned dependency\n")
            run_git("add",".")
            run_git("commit","-m","fixture")
            pinned=run_git("rev-parse","HEAD")
            project=base/"project"
            (project/"scripts").mkdir(parents=True)
            (project/"deploy").mkdir()
            shutil.copy(ROOT/"scripts/prepare.sh",project/"scripts/prepare.sh")
            shutil.copy(ROOT/".env.example",project/".env.example")
            (project/"deploy/ozon-mcp.ref").write_text(pinned+"\n")
            binaries=base/"bin"
            binaries.mkdir()
            wrapper=binaries/"git"
            wrapper.write_text(f"#!{sys.executable}\n"+f'''
import os,sys
args=[{str(upstream)!r} if a=="https://github.com/Alexander-Zhukov/ozon-mcp.git" else a for a in sys.argv[1:]]
os.execv({git!r},[{git!r},*args])
''')
            wrapper.chmod(0o755)
            env={**os.environ,"PATH":str(binaries)+os.pathsep+os.environ["PATH"]}
            def prepare():
                return subprocess.run(["bash",str(project/"scripts/prepare.sh")],env=env,text=True,capture_output=True)
            for _ in range(2):
                result=prepare()
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            actual=subprocess.run([git,"-C",str(project/"vendor/ozon-mcp"),"rev-parse","HEAD"],check=True,text=True,capture_output=True).stdout.strip()
            self.assertEqual(actual,pinned)
            modified=project/"vendor/ozon-mcp/pyproject.toml"
            modified.write_text("# private customization\n")
            result=prepare()
            self.assertNotEqual(result.returncode,0)
            self.assertIn("Local changes",result.stderr)
            self.assertEqual(modified.read_text(),"# private customization\n")


if __name__=="__main__":unittest.main()
