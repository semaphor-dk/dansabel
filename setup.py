import os
from setuptools import setup, find_packages

setup(
    name='dansabel',
    version='1.3.0',
    install_requires=['ruamel.yaml', 'jinja2', 'ansible-core'],
    author='Semaphor',
    author_email='info@semaphor.dk',
    description='Ansible YAML/Jinja2 static analysis tool and pre-commit hook',
    license='ISC',
    keywords='ansible ',
    url='https://github.com/semaphor-dk/dansabel',
    packages=find_packages(),
    include_package_data=True,
    long_description=open('README.md').read(),
    classifiers=[
        'License :: OSI Approved :: ISC license'
    ],
    scripts=['jinjalint.py'],
    data_files=[
    ]
)
