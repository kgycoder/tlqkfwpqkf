from setuptools import setup, find_packages

setup(
    name="lm-arena-cli",
    version="0.1.0",
    description="Claude Code 스타일의 LM Arena Direct 모드 자동화 CLI",
    packages=find_packages(),
    install_requires=[
        "playwright>=1.44.0",
        "rich>=13.7.0",
        "prompt_toolkit>=3.0.0",
        "flask>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "lmarena=lm_arena_cli.cli:main",
        ],
    },
    python_requires=">=3.9",
    include_package_data=True,
    package_data={"lm_arena_cli": ["web_static/*"]},
)