#!/usr/bin/env python
#!/usr/bin/env python
import os, sys, subprocess
import click
import requests
import json
import logging
import contextlib
import ssl
import datetime
import urllib3

try:
    from http.client import HTTPConnection # py3
except ImportError:
    from httplib import HTTPConnection # py2

from time import sleep


@click.command()
@click.option('--rancher-url', envvar='RANCHER_URL', required=True,
              help='The URL for your Rancher server, eg: http://rancher:8000')
@click.option('--rancher-key', envvar='RANCHER_ACCESS_KEY', required=True,
              help="The environment or account API key")
@click.option('--rancher-secret', envvar='RANCHER_SECRET_KEY', required=True,
              help="The secret for the access API key")
@click.option('--cluster', default=None, required=True,
              help="The name of the cluster in Rancher")
@click.option('--environment', default=None, required=True,
              help="The name of the environment to add the host into " + \
                   "(only needed if you are using an account API key instead of an environment API key)")
@click.option('--stack', 'stack_name', envvar='CI_PROJECT_NAMESPACE', default=None, required=True,
              help="The name of the stack in Rancher (defaults to the name of the group in GitLab)")
@click.option('--service', envvar='CI_PROJECT_NAME', default=None, required=True,
              help="The name of the service in Rancher to upgrade (defaults to the name of the service in GitLab)")
@click.option('--upgrade-timeout', default=5*60,
              help="How long to wait, in seconds, for the upgrade to finish before exiting. To skip the wait, pass the --no-wait-for-upgrade-to-finish option.")
@click.option('--wait-for-upgrade-to-finish/--no-wait-for-upgrade-to-finish', default=True,
              help="Wait for Rancher to finish the upgrade before this tool exits")
@click.option('--new-image', default=None,
              help="If specified, replace the image (and :tag) with this one during the upgrade")
@click.option('--debug/--no-debug', default=True,
              help="Enable HTTP Debugging")
def main(rancher_url, rancher_key, rancher_secret, cluster, environment, stack_name, service, new_image, upgrade_timeout, wait_for_upgrade_to_finish, debug):
        """Performs an in service upgrade of the service specified on the command line"""

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        if debug:
                debug_requests_on()

        # split url to protocol and host
        if "://" not in rancher_url:
                bail("The Rancher URL doesn't look right")

        proto, host = rancher_url.split("://")
        api = "%s://%s/v3" % (proto, host)

        # 0 -> Authenticate all future requests
        session = requests.Session()
        session.auth = (rancher_key, rancher_secret)

        # 1 -> Find the cluster id in Rancher
        try:
                r = session.get("%s/clusters?limit=-1" % api, verify=False)
                r.raise_for_status()
        except requests.exceptions.HTTPError:
                bail("Unable to connect to Rancher at %s - is the URL and API key right?" % host)
        else:
                clusters = r.json()['data']

        cluster_id = None
        for c in clusters:
                if c['id'].lower() == cluster.lower() or c['name'].lower() == cluster.lower():
                        cluster_id = c['id']
                        cluster_name = c['name']
                        break

        if not cluster_id:
                if cluster:
                        bail("The '%s' cluster doesn't exist in Rancher, or your API credentials don't have access to it" % cluster)
                else:
                        bail("No cluster in Rancher matches your request")

        # 2 -> Find the environment id in Rancher
        try:
                r = session.get("%s/projects?limit=-1&clusterId=%s" % (api,cluster_id), verify=False)
                r.raise_for_status()
        except requests.exceptions.HTTPError:
                bail("Unable to connect to Rancher at %s - is the URL and API key right?" % host)
        else:
                environments = r.json()['data']

        for e in environments:
                if e['id'].lower() == environment.lower() or e['name'].lower() == environment.lower():
                        environment_id = e['id']
                        environment_name = e['name']
                        break

        if not environment_id:
                if environment:
                        bail("The '%s' environment doesn't exist in Rancher, or your API credentials don't have access to it" % environment)
                else:
                        bail("No environment in Rancher matches your request")

        # 3 -> Find the stack in the environment
        try:
                r = session.get("%s/cluster/%s/namespaces?limit=-1&projectId=%s" % (
                        api,
                        cluster_id,
                        environment_id
                ), verify=False)
                r.raise_for_status()
        except requests.exceptions.HTTPError:
                bail("Unable to fetch a list of stacks in the environment '%s'" % environment_name)
        else:
                stacks = r.json()['data']

        stack = None
        for s in stacks:
                if s['name'].lower() == stack_name.lower():
                        stack = s
                        break

        if not stack:
                bail("Unable to find a stack called '%s'. Does it exist in the '%s' environment?" % (stack_name, environment_name))

        # 4 -> Find the service in the stack
        try:
                r = session.get("%s/projects/%s/workloads?limit=-1&namespaceId=%s" % (
                        api,
                        environment_id,
                        stack['id']
                ), verify=False)
                r.raise_for_status()
        except requests.exceptions.HTTPError:
                bail("Unable to fetch a list of services in the stack. Does your API key have the right permissions?")
        else:
                services = r.json()['data']

        for s in services:
                if s['name'].lower() == service.lower():
                        service = s
                        break
        else:
                bail("Unable to find a service called '%s', does it exist in Rancher?" % service)

        # 5 -> Is the service elligible for upgrade?
        if service['state'] != 'active':
                bail("Unable to start upgrade: current service state '%s', but it needs to be 'active'" % service['state'])

        # 6 -> Start the upgrade
        msg("Upgrading %s/%s in environment %s of cluster %s..." % (stack['name'], service['name'], environment_name, cluster_name))
        upgrade = s;
        upgrade['annotations']['gitlab.com/updateTime'] = datetime.datetime.today().strftime("%Y%m%d%H%M%S");

        if new_image:
                upgrade['containers'][0]['image'] = '%s' % new_image

        try:
                r = session.put(service['links']['self'], json=upgrade, verify=False)
                r.raise_for_status()
        except requests.exceptions.HTTPError:
                bail("Unable to request an upgrade on Rancher")

        # 7 -> Wait for the upgrade to finish
        if not wait_for_upgrade_to_finish:
                msg("Upgrade started")
        else:
                msg("Upgrade started, waiting for upgrade to complete...")
                attempts = 0
                while True:
                        sleep(2)
                        attempts += 2
                        if attempts > upgrade_timeout:
                                bail("A timeout occured while waiting for Rancher to complete the upgrade")
                        try:
                                r = session.get(service['links']['self'], verify=False)
                                r.raise_for_status()
                        except requests.exceptions.HTTPError:
                                bail("Unable to fetch the service status from the Rancher API")
                        else:
                                service = r.json()

                        if service['state'] == "active":
                                break;

                msg("Upgrade finished")
        sys.exit(0)

def msg(msg):
    click.echo(click.style(msg, fg='green'))

def warn(msg):
    click.echo(click.style(msg, fg='yellow'))

def bail(msg):
    click.echo(click.style('Error: ' + msg, fg='red'))
    sys.exit(1)

def debug_requests_on():
    '''Switches on logging of the requests module.'''
    HTTPConnection.debuglevel = 1

    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True
