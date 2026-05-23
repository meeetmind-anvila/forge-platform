from setuptools import setup, find_packages

setup(
    name="forge-cli",
    version="1.0.0",
    description="Forge CI/CD platform CLI",
    packages=find_packages(),
    py_modules=["forge"],
    install_requires=[
        "click>=8.1.0",
        "requests>=2.32.0",
        "sseclient-py>=1.8.0",
    ],
    entry_points={
        "console_scripts": [
            "forge=forge:cli",
        ],
    },
    python_requires=">=3.10",
)