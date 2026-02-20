from setuptools import find_packages, setup


setup(
    name="context-agent-local",
    version="0.1.0",
    description="Local project context memory recorder for Cursor and Claude.",
    packages=find_packages(include=["context_agent", "context_agent.*"]),
    python_requires=">=3.9",
    entry_points={
        "console_scripts": [
            "ctx=context_agent.cli:main",
        ]
    },
)

