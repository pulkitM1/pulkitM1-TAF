import base64
import datetime
import time
import hashlib
import hmac
import json
import httplib2
from global_vars import logger
http = httplib2.Http(timeout=600, disable_ssl_certificate_validation=True)

class pod:
    def __init__(self, url, url_public):
        self.url = url
        self.url_public = url_public

class tenant:
    def __init__(self, id, user, pwd):
        self.id = id
        self.user = user
        self.pwd = pwd
        self.api_secret_key = None
        self.api_access_key = None
        self.project_id = None
        self.clusters = dict()

class capella_utils():
    cidr = "10.0.0.0"
    memcached_port = "11207"
    log = logger.get("infra")
    jwt = None

    @staticmethod
    def get_authorization_internal(pod, tenant):
        if capella_utils.jwt is None:
            basic = base64.encodestring('{}:{}'.format(tenant.user, tenant.pwd).encode('utf-8')).decode('utf-8')
            _, content = http.request("{}/sessions".format(pod.url), method="POST", headers={"Authorization": "Basic %s" % basic})
            capella_utils.jwt = json.loads(content).get("jwt")
        cbc_api_request_headers = {
           'Authorization' : 'Bearer %s' % capella_utils.jwt,
           'Content-Type': 'application/json'
        }
        return cbc_api_request_headers

    @staticmethod
    def get_authorization_v3(cbc_api_method, cbc_api_endpoint):
        # Epoch time in milliseconds
        cbc_api_now =  int(datetime.datetime.now().timestamp() * 1000)

        # Form the message string for the Hmac hash
        cbc_api_message= cbc_api_method + '\n' + cbc_api_endpoint + '\n' + str(cbc_api_now)

        # Calculate the hmac hash value with secret key and message
        cbc_api_signature = base64.b64encode(hmac.new(bytes(self.api_secret_key, 'utf-8'), bytes(cbc_api_message,'utf-8'), digestmod=hashlib.sha256).digest())

        # Values for the header
        cbc_api_request_headers = {
           'Authorization' : 'Bearer ' + self.api_access_key + ':' + cbc_api_signature.decode() ,
           'Couchbase-Timestamp' : str(cbc_api_now),
        }
        return cbc_api_request_headers

    @staticmethod
    def create_project(pod, tenant, name):
        project_details = {"name": name, "tenantId": tenant.id}

        uri = '{}/v2/organizations/{}/projects'.format(pod.url, tenant.id)
        capella_header = capella_utils.get_authorization_internal(pod, tenant)
        response, content = http.request(uri, method="POST", body=json.dumps(project_details), headers=capella_header)
        project_id = json.loads(content).get("id")
        tenant.project_id = project_id
        capella_utils.log.info("Project ID: {}".format(project_id))

    @staticmethod
    def delete_project(pod, tenant):
        uri = '{}/v2/organizations/{}/projects/{}'.format(pod.url, tenant.id, tenant.project_id)
        response, content = http.request(uri, method="DELETE", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
        capella_utils.log.info("Project Deleted: {}".format(tenant.project_id))

    @staticmethod
    def get_next_cidr():
        addr = capella_utils.cidr.split(".")
        addr[1] = str(int(addr[1]) + 1)
        capella_utils.cidr = ".".join(addr)
        return capella_utils.cidr

    @staticmethod
    def create_cluster(pod, tenant, cluster_details):
        while True:
            subnet = capella_utils.get_next_cidr() + "/20"
            capella_utils.log.info("Trying with cidr: {}".format(subnet))
            cluster_details.update({"cidr": subnet, "projectId":tenant.project_id})
            uri = '{}/v2/organizations/{}/clusters'.format(pod.url, tenant.id)
            response, content = http.request(uri, method="POST", body=json.dumps(cluster_details), headers=capella_utils.get_authorization_internal(pod, tenant))
            capella_utils.log.info(content)
            if int(response.get("status")) >= 200 and int(response.get("status")) < 300 :
                capella_utils.log.info("Cluster created successfully!")
                break

        cluster_id = json.loads(content).get("id")
        capella_utils.log.info("Cluster created with cluster ID: {}".format(cluster_id))
        capella_utils.wait_until_done(pod, tenant, cluster_id, "Creating Cluster")
        cluster_srv = capella_utils.get_cluster_srv(pod, tenant, cluster_id)
        capella_utils.add_allowed_ip(pod, tenant, cluster_id)
        servers = capella_utils.get_nodes(pod, tenant, cluster_id)
        return cluster_id, cluster_srv, servers

    @staticmethod
    def wait_until_done(pod, tenant, cluster_id, msg="", prnt=False):
        while True:
            try:
                content = capella_utils.jobs(pod, tenant, cluster_id)
                state = capella_utils.get_cluster_state(pod, tenant, cluster_id)
                if prnt:
                    capella_utils.log.info(content)
                if content.get("data") or state != "healthy":
                    for data in content.get("data"):
                        data = data.get("data")
                        if data.get("clusterId") == cluster_id:
                            step, progress = data.get("currentStep"), data.get("completionPercentage")
                            capella_utils.log.info("{}: Status=={}, State=={}, Progress=={}%".format(msg, state, step, progress))
                    time.sleep(2)
                else:
                    capella_utils.log.info("{} Ready!!!".format(msg))
                    break
            except:
                capella_utils.log.info("ERROR!!!")
                break

    @staticmethod
    def destroy_cluster(pod, tenant, cluster):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster.id)
        _, content = http.request(base_url_internal, method="DELETE", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
        time.sleep(10)

        while True:
            response, content = http.request(base_url_internal, method="GET", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
            content = json.loads(content)
            if content.get("data"):
                capella_utils.log.info("Cluster status: {}".format(content.get("data").get("status").get("state")))
                if content.get("data").get("status").get("state") == "destroying":
                    time.sleep(5)
                    continue
            elif content.get("message") == 'Not Found.':
                capella_utils.log.info("Cluster is destroyed.")
                tenant.clusters.pop(cluster.id)
                break

    @staticmethod
    def create_bucket(pod, tenant, cluster, bucket_params={}):
        while True:
            state = capella_utils.get_cluster_state(pod, tenant, cluster.id)
            if state == "healthy":
                break
            time.sleep(1)
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster.id)
        uri = '{}/buckets'.format(base_url_internal)
        default = {"name":"default","bucketConflictResolution":"seqno","memoryAllocationInMb":100,"flush":False,"replicas":0,"durabilityLevel":"none","timeToLive":None}
        default.update(bucket_params)
        response, content = http.request(uri, method="POST", body=json.dumps(default), headers=capella_utils.get_authorization_internal(pod, tenant))
        if int(response.get("status")) >= 200 and int(response.get("status")) < 300 :
            capella_utils.log.info("Bucket create successfully!")

    @staticmethod
    def delete_bucket(pod, tenant, cluster, name):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster.id)
        uri = '{}/buckets'.format(base_url_internal)
        response, content = http.request(uri, method="GET", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
        content = json.loads(content)
        bucket_id = None
        for bucket in content.get("buckets").get("data"):
                if bucket.get("data").get("name") == name:
                        bucket_id = bucket.get("data").get("id")
        if bucket_id:
            uri = uri + "/" + bucket_id
            response, content = http.request(uri, method="DELETE", headers=capella_utils.get_authorization_internal(pod, tenant))
            if int(response.get("status")) >= 200 and int(response.get("status")) < 300 :
                capella_utils.log.info("Bucket deleted successfully!")
            else:
                capella_utils.log.info(content)
        else:
            capella_utils.log.info("Bucket not found.")

    @staticmethod
    def scale(pod, tenant, cluster, scale_params):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster.id)
        uri = '{}/specs'.format(base_url_internal)
        scale_params = json.dumps(scale_params)
        print(scale_params, uri)
        response, content = http.request(uri, method="POST", body=scale_params, headers=capella_utils.get_authorization_internal(pod, tenant))
        return response, content
        # time.sleep(10)
        # capella_utils.wait_until_done(pod, tenant, cluster.id, "Scaling Operation")

    @staticmethod
    def jobs(pod, tenant, cluster_id):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster_id)
        uri = '{}/jobs'.format(base_url_internal)
        response, content = http.request(uri, method="GET", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
        return json.loads(content)

    @staticmethod
    def get_cluster_details(pod, cluster_id):
        endpoint = '/v3/clusters/{}'.format(cluster_id)
        uri = pod.url_public + endpoint
        response, content = http.request(uri, method="GET", body='', headers=capella_utils.get_authorization_v3("GET", endpoint))
        return json.loads(content)

    @staticmethod
    def get_cluster_info(pod, tenant, cluster_id):
        uri = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster_id)
        response, content = http.request(uri, method="GET", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
        return json.loads(content)

    @staticmethod
    def get_cluster_state(pod, tenant, cluster_id):
        content = capella_utils.get_cluster_info(pod, tenant, cluster_id)
        return content.get("data").get("status").get("state")

    @staticmethod
    def get_cluster_srv(pod, tenant, cluster_id):
        content = capella_utils.get_cluster_info(pod, tenant, cluster_id)
        return content.get("data").get("connect").get("srv")

    @staticmethod
    def get_nodes(pod, tenant, cluster_id):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster_id)
        uri = '{}/nodes'.format(base_url_internal)
        response, content = http.request(uri, method="GET", body='', headers=capella_utils.get_authorization_internal(pod, tenant))
        capella_utils.log.info(json.loads(content))
        return [server.get("data") for server in json.loads(content).get("data")]

    @staticmethod
    def get_db_users(pod, tenant, cluster_id, page=1, limit=100):
        header = capella_utils.get_authorization_internal(pod, tenant)
        uri = '{}/v2/organizations/{}/projects/{}/clusters/{}' \
              .format(pod.url, tenant.id, tenant.project_id, cluster_id)
        uri = uri + '/users?page=%s&perPage=%s' % (page, limit)
        response, content = http.request(uri, method="GET", headers=header)
        return json.loads(content)

    @staticmethod
    def delete_db_user(pod, tenant, cluster_id, user_id):
        uri = "{}/v2/organizations/{}/projects/{}/clusters/{}/users/{}" \
              .format(pod.url, tenant.id, tenant.project_id, cluster_id,
                      user_id)
        print(uri)

    @staticmethod
    def create_db_user(pod, tenant, cluster_id, user, pwd):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster_id)
        body = {"name":user,"password":pwd,"permissions":{"data_reader":{},"data_writer":{}}}
        uri = '{}/users'.format(base_url_internal)
        response, content = http.request(uri, method="POST", body=json.dumps(body), headers=capella_utils.get_authorization_internal(pod, tenant))
        capella_utils.log.info(json.loads(content))
        return json.loads(content)

    @staticmethod
    def add_allowed_ip(pod, tenant, cluster_id):
        base_url_internal = '{}/v2/organizations/{}/projects/{}/clusters/{}'.format(pod.url, tenant.id, tenant.project_id, cluster_id)
        _, content = http.request("https://ifconfig.me/all.json", method="GET")
        ip = json.loads(content).get("ip_addr")
        body = {"create":[{"cidr":"{}/32".format(ip),"comment":""}]}
        uri = '{}/allowlists-bulk'.format(base_url_internal)
        _, content = http.request(uri, method="POST", body=json.dumps(body), headers=capella_utils.get_authorization_internal(pod, tenant))

    @staticmethod
    def load_sample_bucket(pod, tenant, cluster_id, bucket_name):
        header = capella_utils.get_authorization_internal(pod, tenant)
        uri = "{}/v2/organizations/{}/projects/{}/clusters/{}/buckets/samples" \
              .format(pod.url, tenant.id, tenant.project_id, cluster_id)
        param = {'name': bucket_name}
        response, content = http.request(uri, method="POST",
                                         body=json.dumps(param),
                                         headers=header)
