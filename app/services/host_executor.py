import os
import tempfile
import asyncio
import subprocess
from typing import Dict, Any, Optional

class HostExecutionService:
    """
    Executes code directly on the host machine.
    Lightning fast, no sandbox. Perfect for local dev/hackathons.
    """
    def __init__(self):
        self.TIMEOUT = 10

    async def execute_code(self, code: str, language: str, stdin: Optional[str] = None) -> Dict[str, Any]:
        tmp_dir = tempfile.mkdtemp(prefix="host_exec_")
        
        try:
            filename = self._get_filename(language)
            filepath = os.path.join(tmp_dir, filename)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(code)

            cmd = self._get_execution_cmd(language, filepath, tmp_dir)
            if not cmd:
                return {"success": False, "error": f"Language {language} is not supported on Windows host.", "status": "error"}

            # For compiled languages like C/C++, compile first
            if language in ["c", "cpp"]:
                compile_cmd = cmd[0]
                run_cmd = cmd[1]
                
                # Compile step
                try:
                    c_proc = await asyncio.to_thread(
                        subprocess.run,
                        compile_cmd,
                        capture_output=True,
                        cwd=tmp_dir,
                        timeout=10,
                        text=False,
                    )
                except subprocess.TimeoutExpired:
                    return {
                        "success": False,
                        "output": None,
                        "error": "Compilation timed out after 10s",
                        "status": "timeout"
                    }
                
                if c_proc.returncode != 0:
                    return {
                        "success": False,
                        "output": c_proc.stdout.decode("utf-8", errors="replace"),
                        "error": c_proc.stderr.decode("utf-8", errors="replace"),
                        "status": "runtime_error"
                    }
                
                cmd = run_cmd

            # Run step
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    input=stdin.encode() if stdin else None,
                    capture_output=True,
                    cwd=tmp_dir,
                    timeout=self.TIMEOUT,
                    text=False,
                )
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "output": None,
                    "error": f"Execution timed out after {self.TIMEOUT}s",
                    "status": "timeout"
                }

            stdout_str = proc.stdout.decode("utf-8", errors="replace").strip()
            stderr_str = proc.stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return {
                    "success": True,
                    "output": stdout_str if stdout_str else "(Execution completed successfully with no terminal output.)",
                    "error": stderr_str if stderr_str else None,
                    "status": "success",
                    "time": None,
                    "memory": None
                }
            else:
                return {
                    "success": False,
                    "output": stdout_str,
                    "error": stderr_str,
                    "status": "runtime_error",
                    "time": None,
                    "memory": None
                }

        finally:
            import shutil
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass


    def _get_filename(self, language: str) -> str:
        ext_map = {
            "python": "main.py",
            "javascript": "main.js",
            "typescript": "main.js", # Run ts as js for simplicity unless ts-node is mapped
            "java": "Main.java",
            "cpp": "main.cpp",
            "c": "main.c",
        }
        return ext_map.get(language.lower(), "main.txt")

    def _get_execution_cmd(self, language: str, filepath: str, tmp_dir: str):
        if language == "python":
            return ["python", filepath]
        elif language in ["javascript", "typescript"]:
            return ["node", filepath]
        elif language == "c":
            out_exe = os.path.join(tmp_dir, "a.exe")
            return (["gcc", filepath, "-o", out_exe], [out_exe])
        elif language == "cpp":
            out_exe = os.path.join(tmp_dir, "a.exe")
            return (["g++", filepath, "-o", out_exe], [out_exe])
        elif language == "java":
            # Just try executing the Java file natively starting from JDK 11
            return ["java", filepath]
        return None

host_execution_service = HostExecutionService()
