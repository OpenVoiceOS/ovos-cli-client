import os

from setuptools import setup


def package_files(directory):
    paths = []
    for (path, directories, filenames) in os.walk(directory):
        for filename in filenames:
            paths.append(os.path.join('..', path, filename))
    return paths


setup(
    name='ovos-cli-client',
    version="0.0.1",
    packages=['ovos_cli_client'],
    url='https://github.com/OpenVoiceOS/ovos_cli_client',
    install_requires=["ovos_utils>=0.0.25a8"],
    package_data={'': package_files('ovos_cli_client')},
    include_package_data=True,
    license='Apache',
    author='jarbasAI',
    author_email='jarbasai@mailfence.com',
    description='ovos-core debug cli client',
    entry_points={
        'console_scripts': [
            'ovos-cli-client=ovos_cli_client.__main__:main'
        ]
    }
)
