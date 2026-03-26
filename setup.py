from setuptools import setup, find_packages

setup(
    name="pulse",
    version="0.1.0",
    description="AI Harness 竞品情报系统",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "click>=8.0",
        "pyyaml>=6.0",
        "fastapi>=0.100",
        "uvicorn[standard]>=0.20",
        "croniter>=2.0",
        "requests>=2.28",
    ],
    entry_points={
        "console_scripts": [
            "pulse=pulse.main:cli",
        ],
    },
)
