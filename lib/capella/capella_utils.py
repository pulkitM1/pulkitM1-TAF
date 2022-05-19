import time
import json
from global_vars import logger
from capellaAPI.CapellaAPI import CapellaAPI


class Pod:
    def __init__(self, url, url_public):
        self.url = url
        self.url_public = url_public


class Tenant:
    def __init__(self, id, user, pwd,
                 secret=None, access=None):
        self.id = id
        self.user = user
        self.pwd = pwd
        self.api_secret_key = secret
        self.api_access_key = access
        self.project_id = None
        self.clusters = dict()


class CapellaUtils(object):
    cidr = "10.0.0.0"
    memcached_port = "11207"
    log = logger.get("infra")

    @staticmethod
    def create_project(pod, tenant, name):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.create_project(pod.url, tenant.id, name)
        if resp.status_code != 201:
            raise Exception("Creating capella project failed.")
        project_id = json.loads(resp.content).get("id")
        tenant.project_id = project_id
        CapellaUtils.log.info("Project ID: {}".format(project_id))

    @staticmethod
    def delete_project(pod, tenant):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        capella_api.delete_project(pod.url, tenant.id, tenant.project_id)
        CapellaUtils.log.info("Project Deleted: {}".format(tenant.project_id))

    @staticmethod
    def get_next_cidr():
        addr = CapellaUtils.cidr.split(".")
        if int(addr[1]) < 255:
            addr[1] = str(int(addr[1]) + 1)
        elif int(addr[2]) < 255:
            addr[2] = str(int(addr[2]) + 1)
        CapellaUtils.cidr = ".".join(addr)
        return CapellaUtils.cidr

    @staticmethod
    def create_cluster(pod, tenant, cluster_details, timeout=1800):
        end_time = time.time() + timeout
        while time.time() < end_time:
            subnet = CapellaUtils.get_next_cidr() + "/20"
            CapellaUtils.log.info("Trying with cidr: {}".format(subnet))
            cluster_details["place"]["hosted"].update({"CIDR": subnet})
            cluster_details.update({"projectId": tenant.project_id})
            capella_api = CapellaAPI(pod.url_public,
                                     tenant.api_secret_key,
                                     tenant.api_access_key,
                                     tenant.user,
                                     tenant.pwd)
            capella_api_resp = capella_api.create_cluster(cluster_details)

            # Check resp code , 202 is success
            if capella_api_resp.status_code == 202:
                break
            else:
                CapellaUtils.log.critical("Create capella cluster failed.")
                CapellaUtils.log.critical("Capella API returned " + str(
                    capella_api_resp.status_code))
                CapellaUtils.log.critical(capella_api_resp.json()["message"])

        cluster_id = capella_api_resp.headers['Location'].split("/")[-1]
        CapellaUtils.log.info("Cluster created with cluster ID: {}"\
                              .format(cluster_id))
        CapellaUtils.wait_until_done(pod, tenant, cluster_id,
                                     "Creating Cluster {}".format(
                                         cluster_details.get("clusterName")))
        cluster_srv = CapellaUtils.get_cluster_srv(pod, tenant, cluster_id)
        CapellaUtils.add_allowed_ip(pod, tenant, cluster_id)
        servers = CapellaUtils.get_nodes(pod, tenant, cluster_id)
        return cluster_id, cluster_srv, servers

    @staticmethod
    def wait_until_done(pod, tenant, cluster_id, msg="", prnt=False,
                        timeout=1800):
        end_time = time.time() + timeout
        while time.time() < end_time:
            content = CapellaUtils.jobs(pod, tenant, cluster_id)
            state = CapellaUtils.get_cluster_state(pod, tenant, cluster_id)
            if state in ["deployment_failed",
                         "deploymentFailed",
                         "redeploymentFailed",
                         "rebalance_failed"]:
                raise Exception("{} for cluster {}".format(
                    state, cluster_id))
            if prnt:
                CapellaUtils.log.info(content)
            if content.get("data") or state != "healthy":
                for data in content.get("data"):
                    data = data.get("data")
                    if data.get("clusterId") == cluster_id:
                        step, progress = data.get("currentStep"), \
                                         data.get("completionPercentage")
                        CapellaUtils.log.info(
                            "{}: Status=={}, State=={}, Progress=={}%"
                            .format(msg, state, step, progress))
                time.sleep(2)
            else:
                CapellaUtils.log.info("{} Ready!!!".format(msg))
                break

    @staticmethod
    def destroy_cluster(pod, tenant, cluster):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.delete_cluster(cluster.id)
        if resp.status_code != 202:
            raise Exception("Deleting Capella Cluster Failed.")

        time.sleep(10)
        while True:
            resp = capella_api.get_cluster_internal(pod.url, tenant.id,
                                                    tenant.project_id,
                                                    cluster.id)
            content = json.loads(resp.content)
            if content.get("data"):
                CapellaUtils.log.info(
                    "Cluster status %s: %s"
                    % (cluster.cluster_config.get("name"),
                       content.get("data").get("status").get("state")))
                if content.get("data").get("status").get("state") == "destroying":
                    time.sleep(5)
                    continue
            elif content.get("message") == 'Not Found.':
                CapellaUtils.log.info("Cluster is destroyed.")
                tenant.clusters.pop(cluster.id)
                break

    @staticmethod
    def create_bucket(pod, tenant, cluster, bucket_params={}):
        while True:
            state = CapellaUtils.get_cluster_state(pod, tenant, cluster.id)
            if state == "healthy":
                break
            time.sleep(1)
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.create_bucket(pod.url, tenant.id, tenant.project_id,
                                         cluster.id, bucket_params)
        if resp.status_code in [200, 201, 202]:
            CapellaUtils.log.info("Bucket create successfully!")

    @staticmethod
    def get_bucket_id(pod, tenant, cluster, name):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.get_buckets(pod.url, tenant.id, tenant.project_id, cluster.id)
        content = json.loads(resp.content)
        bucket_id = None
        for bucket in content.get("buckets").get("data"):
                if bucket.get("data").get("name") == name:
                        bucket_id = bucket.get("data").get("id")
        return bucket_id

    @staticmethod
    def flush_bucket(pod, tenant, cluster, name):
        bucket_id = CapellaUtils.get_bucket_id(pod, tenant, cluster, name)
        if bucket_id:
            capella_api = CapellaAPI(pod.url_public,
                                     tenant.api_secret_key,
                                     tenant.api_access_key,
                                     tenant.user,
                                     tenant.pwd)
            resp = capella_api.flush_bucket(pod.url, tenant.id,
                                            tenant.project_id, cluster.id,
                                            bucket_id)
            if resp.status >= 200 and resp.status < 300:
                CapellaUtils.log.info("Bucket deleted successfully!")
            else:
                CapellaUtils.log.info(resp.content)
        else:
            CapellaUtils.log.info("Bucket not found.")

    @staticmethod
    def delete_bucket(pod, tenant, cluster, name):
        bucket_id = CapellaUtils.get_bucket_id(pod, tenant, cluster, name)
        if bucket_id:
            capella_api = CapellaAPI(pod.url_public,
                                     tenant.api_secret_key,
                                     tenant.api_access_key,
                                     tenant.user,
                                     tenant.pwd)
            resp = capella_api.delete_bucket(pod.url, tenant.id,
                                             tenant.project_id, cluster.id,
                                             bucket_id)
            if resp.status_code == 204:
                CapellaUtils.log.info("Bucket deleted successfully!")
            else:
                CapellaUtils.log.critical(resp.content)
                raise Exception("Bucket {} cannot be deleted".format(name))
        else:
            CapellaUtils.log.info("Bucket not found.")

    @staticmethod
    def update_bucket_settings(pod, tenant, cluster, bucket_id, bucket_params):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.update_bucket_settings(pod.url,
                                                  tenant.id, tenant.project_id,
                                                  cluster.id, bucket_id,
                                                  bucket_params)
        code = resp.status
        if 200 > code or code >= 300:
            CapellaUtils.log.critical("Bucket update failed: %s" % resp.content)
        return resp.status

    @staticmethod
    def scale(pod, tenant, cluster, new_config):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        while True:
            resp = capella_api.update_cluster_servers(cluster.id, new_config)
            if resp.status_code != 202:
                result = json.loads(resp.content)
                if result["errorType"] == "ClusterModifySpecsInvalidState":
                    CapellaUtils.wait_until_done(pod, tenant, cluster.id,
                                                 "Wait for healthy cluster state")
            else:
                break

    @staticmethod
    def jobs(pod, tenant, cluster_id):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.jobs(pod.url, tenant.project_id, tenant.id, cluster_id)
        if resp.status_code != 200:
            if resp.status_code == 502:
                CapellaUtils.log.critical("LOG A BUG: Internal API returns :\
                {}".format(json.loads(resp.content)))
                return CapellaUtils.jobs(pod, tenant, cluster_id)
            raise Exception("Fetch capella cluster jobs failed: %s"
                            % resp.content)
        return json.loads(resp.content)

    @staticmethod
    def get_cluster_info(pod, tenant, cluster_id):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.get_cluster_info(cluster_id)
        if resp.status_code != 200:
            raise Exception("Fetch capella cluster details failed!")
        return json.loads(resp.content)

    @staticmethod
    def get_cluster_state(pod, tenant, cluster_id):
        content = CapellaUtils.get_cluster_info(pod, tenant, cluster_id)
        return content.get("status")

    @staticmethod
    def get_cluster_srv(pod, tenant, cluster_id):
        content = CapellaUtils.get_cluster_info(pod, tenant, cluster_id)
        return content.get("endpointsSrv")

    @staticmethod
    def get_nodes(pod, tenant, cluster_id):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.get_nodes(pod.url, tenant.id, tenant.project_id,
                                     cluster_id)
        if resp.status_code != 200:
            raise Exception("Fetch capella cluster nodes failed!")
        CapellaUtils.log.info(json.loads(resp.content))
        return [server.get("data")
                for server in json.loads(resp.content).get("data")]

    @staticmethod
    def get_db_users(pod, tenant, cluster_id, page=1, limit=100):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.get_db_users(pod.url, tenant.id, tenant.project_id,
                                        cluster_id, page, limit)
        return json.loads(resp.content)

    @staticmethod
    def delete_db_user(pod, tenant, cluster_id, user_id):
        uri = "{}/v2/organizations/{}/projects/{}/clusters/{}/users/{}" \
              .format(pod.url, tenant.id, tenant.project_id, cluster_id,
                      user_id)
        print(uri)

    @staticmethod
    def create_db_user(pod, tenant, cluster_id, user, pwd):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.create_db_user(pod.url, tenant.id, tenant.project_id,
                                          cluster_id, user, pwd)
        if resp.status_code != 200:
            result = json.loads(resp.content)
            if result["errorType"] == "ErrDataplaneUserNameExists":
                CapellaUtils.log.warn("User is already added: %s" % result["message"])
                return
            raise Exception("Add capella cluster user failed!")
            CapellaUtils.log.critical(json.loads(resp.content))
        CapellaUtils.log.info(json.loads(resp.content))
        return json.loads(resp.content)

    @staticmethod
    def add_allowed_ip(pod, tenant, cluster_id):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.add_allowed_ip(pod.url, tenant.id, tenant.project_id,
                                          cluster_id)
        if resp.status_code != 202:
            result = json.loads(resp.content)
            if result["errorType"] == "ErrAllowListsCreateDuplicateCIDR":
                CapellaUtils.log.warn("IP is already added: %s" % result["message"])
                return
            CapellaUtils.log.critical(resp.content)
            raise Exception("Adding allowed IP failed.")

    @staticmethod
    def load_sample_bucket(pod, tenant, cluster_id, bucket_name):
        capella_api = CapellaAPI(pod.url_public,
                                 tenant.api_secret_key,
                                 tenant.api_access_key,
                                 tenant.user,
                                 tenant.pwd)
        resp = capella_api.load_sample_bucket(pod.url, tenant.id, tenant.project_id,
                                              cluster_id, bucket_name)