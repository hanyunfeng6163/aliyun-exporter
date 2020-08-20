import json

from aliyunsdkcore.client import AcsClient
from cachetools import cached, TTLCache
from prometheus_client.metrics_core import GaugeMetricFamily

import aliyunsdkecs.request.v20140526.DescribeInstancesRequest as DescribeECS
import aliyunsdkrds.request.v20140815.DescribeDBInstancesRequest as DescribeRDS
import aliyunsdkr_kvstore.request.v20150101.DescribeInstancesRequest as DescribeRedis
import aliyunsdkslb.request.v20140515.DescribeLoadBalancersRequest as DescribeSLB
import aliyunsdkdds.request.v20151201.DescribeDBInstancesRequest as Mongodb
import aliyunsdkpolardb.request.v20170801.DescribeDBClustersRequest as Polardb
import oss2

from aliyunsdkdts.request.v20200101 import DescribeMigrationJobsRequest
from aliyunsdkdts.request.v20200101 import DescribeSubscriptionInstancesRequest
from aliyunsdkons.request.v20190214 import OnsInstanceInServiceListRequest
from aliyunsdkelasticsearch.request.v20170613 import ListInstanceRequest as ElasticSearch
# from aliyunsdkvpc.request.v20160428 import DescribeEipAddressesRequest

from aliyun_exporter.utils import try_or_else

cache = TTLCache(maxsize=100, ttl=3600)

'''
InfoProvider provides the information of cloud resources as metric.

The result from alibaba cloud API will be cached for an hour. 

Different resources should implement its own 'xxx_info' function. 

Different resource has different information structure, and most of
them are nested, for simplicity, we map the top-level attributes to the
labels of metric, and handle nested attribute specially. If a nested
attribute is not handled explicitly, it will be dropped.
'''


class InfoProvider():

    def __init__(self, ak, secret, region_id):
        self.client = None
        self.ak = ak
        self.secret = secret
        self.region_id = region_id

    @cached(cache)
    def get_metrics(self, resource: str, client: AcsClient) -> GaugeMetricFamily:
        self.client = client
        return {
            'ecs': lambda: self.ecs_info(),
            'rds': lambda: self.rds_info(),
            'redis': lambda: self.redis_info(),
            'slb': lambda: self.slb_info(),
            'mongodb': lambda: self.mongodb_info(),
            'polardb': lambda: self.polardb_info(),
            'oss': lambda: self.oss_info(),
            'dts_migration': lambda: self.dts_migration_info(),
            'dts_subcription': lambda: self.dts_subscription_info(),
            'mq': lambda: self.mq_info(),
            'elasticsearch': lambda: self.elasticsearch_info(),
            # 'eip': lambda: self.eip_info(),
        }[resource]()

    def ecs_info(self) -> GaugeMetricFamily:
        req = DescribeECS.DescribeInstancesRequest()
        nested_handler = {
            'InnerIpAddress': lambda obj: try_or_else(lambda: obj['IpAddress'][0], ''),
            'PublicIpAddress': lambda obj: try_or_else(lambda: obj['IpAddress'][0], ''),
            'VpcAttributes': lambda obj: try_or_else(lambda: obj['PrivateIpAddress']['IpAddress'][0], ''),
        }
        return self.info_template(req, 'aliyun_meta_ecs_info', nested_handler=nested_handler)

    def rds_info(self) -> GaugeMetricFamily:
        req = DescribeRDS.DescribeDBInstancesRequest()
        return self.info_template(req, 'aliyun_meta_rds_info', to_list=lambda data: data['Items']['DBInstance'])

    def redis_info(self) -> GaugeMetricFamily:
        req = DescribeRedis.DescribeInstancesRequest()
        return self.info_template(req, 'aliyun_meta_redis_info',
                                  to_list=lambda data: data['Instances']['KVStoreInstance'])

    def slb_info(self) -> GaugeMetricFamily:
        req = DescribeSLB.DescribeLoadBalancersRequest()
        return self.info_template(req, 'aliyun_meta_slb_info',
                                  to_list=lambda data: data['LoadBalancers']['LoadBalancer'])

    def mongodb_info(self) -> GaugeMetricFamily:
        req = Mongodb.DescribeDBInstancesRequest()
        return self.info_template(req, 'aliyun_meta_mongodb_info',
                                  to_list=lambda data: data['DBInstances']['DBInstance'])

    def polardb_info(self) -> GaugeMetricFamily:
        req = Polardb.DescribeDBClustersRequest()
        return self.info_template(req, 'aliyun_meta_polardb_info', to_list=lambda data: data['Items']['DBCluster'])

    def oss_info(self) -> GaugeMetricFamily:
        auth = oss2.Auth(self.ak, self.secret)
        service = oss2.Service(auth, 'http://oss-{resion_id}.aliyuncs.com'.format(resion_id=self.region_id))
        nested_handler = None
        gauge = None
        label_keys = None
        for instance in oss2.BucketIterator(service):
            bucket = oss2.Bucket(auth, 'http://oss-cn-beijing.aliyuncs.com', instance.name)
            bucket_info = bucket.get_bucket_info()
            instance_dict = {'name': bucket_info.name,
                             'storage_class': bucket_info.storage_class,
                             'creation_date': bucket_info.creation_date,
                             'intranet_endpoint': bucket_info.intranet_endpoint,
                             'extranet_endpoint': bucket_info.extranet_endpoint,
                             'owner': bucket_info.owner.id,
                             'grant': bucket_info.acl.grant,
                             'data_redundancy_type': bucket_info.data_redundancy_type,
                             }
            if gauge == None:
                label_keys = self.label_keys(instance_dict, nested_handler)
                gauge = GaugeMetricFamily('aliyun_meta_oss_info', '', labels=label_keys)
            gauge.add_metric(labels=self.label_values(instance_dict, label_keys, nested_handler), value=1.0)
        return gauge

    def dts_migration_info(self) -> GaugeMetricFamily:
        req = DescribeMigrationJobsRequest.DescribeMigrationJobsRequest()
        return self.new_info_template(req, 'aliyun_meta_dts_migration_info',
                                      to_list=lambda data: data['MigrationJobs']['MigrationJob'])

    def dts_subscription_info(self) -> GaugeMetricFamily:
        req = DescribeSubscriptionInstancesRequest.DescribeSubscriptionInstancesRequest()
        return self.new_info_template(req, 'aliyun_meta_dts_subscription_info',
                                      to_list=lambda data: data['SubscriptionInstances']['SubscriptionInstance'])

    def mq_info(self) -> GaugeMetricFamily:
        req = OnsInstanceInServiceListRequest.OnsInstanceInServiceListRequest()
        resp = self.client.do_action_with_exception(req)
        data = json.loads(resp)
        nested_handler = None
        gauge = None
        label_keys = None
        for i in data['Data']['InstanceVO']:
            if gauge == None:
                label_keys = self.label_keys(i, nested_handler)
                gauge = GaugeMetricFamily('aliyun_meta_mq_info', '', labels=label_keys)
            gauge.add_metric(labels=self.label_values(i, label_keys, nested_handler), value=1.0)
        return gauge

    def elasticsearch_info(self) -> GaugeMetricFamily:
        req = ElasticSearch.ListInstanceRequest()
        return self.es_info_template(req, 'aliyun_meta_elasticsearch_info', to_list=lambda data: data['Result'])

    # def eip_info(self) -> GaugeMetricFamily:
    #     req = DescribeEipAddressesRequest.DescribeEipAddressesRequest()
    #     return self.info_template(req, 'aliyun_meta_eip_info')



    '''
    Template method to retrieve resource information and transform to metric.
    '''

    def info_template(self,
                      req,
                      name,
                      desc='',
                      page_size=100,
                      page_num=1,
                      nested_handler=None,
                      to_list=(lambda data: data['Instances']['Instance'])) -> GaugeMetricFamily:
        gauge = None
        label_keys = None
        for instance in self.pager_generator(req, page_size, page_num, to_list):
            if gauge is None:
                label_keys = self.label_keys(instance, nested_handler)
                gauge = GaugeMetricFamily(name, desc, labels=label_keys)
            gauge.add_metric(labels=self.label_values(instance, label_keys, nested_handler), value=1.0)
        return gauge

    def pager_generator(self, req, page_size, page_num, to_list):
        req.set_PageSize(page_size)
        while True:
            req.set_PageNumber(page_num)
            resp = self.client.do_action_with_exception(req)
            data = json.loads(resp)
            instances = to_list(data)
            for instance in instances:
                yield instance
            if len(instances) < page_size:
                break
            page_num += 1

    def new_info_template(self,
                          req,
                          name,
                          desc='',
                          page_size=100,
                          page_num=1,
                          nested_handler=None,
                          to_list=(lambda data: data['Instances']['Instance'])) -> GaugeMetricFamily:
        """
        为了适配新版本sdk
        :param req:
        :param name:
        :param desc:
        :param page_size:
        :param page_num:
        :param nested_handler:
        :param to_list:
        :return:
        """
        gauge = None
        label_keys = None
        for instance in self.new_pager_generator(req, page_size, page_num, to_list):
            if gauge is None:
                label_keys = self.label_keys(instance, nested_handler)
                gauge = GaugeMetricFamily(name, desc, labels=label_keys)
            gauge.add_metric(labels=self.label_values(instance, label_keys, nested_handler), value=1.0)
        return gauge

    def new_pager_generator(self, req, page_size, page_num, to_list):
        """
        为了适配新版本sdk
        :param req:
        :param page_size:
        :param page_num:
        :param to_list:
        :return:
        """
        req.set_PageSize(page_size)
        while True:
            req.set_PageNum(page_num)
            resp = self.client.do_action_with_exception(req)
            data = json.loads(resp)
            instances = to_list(data)
            for instance in instances:
                yield instance
            if len(instances) < page_size:
                break
            page_num += 1

    def es_info_template(self,
                          req,
                          name,
                          desc='',
                          page_size=100,
                          page_num=1,
                          nested_handler=None,
                          to_list=(lambda data: data['Instances']['Instance'])) -> GaugeMetricFamily:
        """
        为了适配新版本sdk
        :param req:
        :param name:
        :param desc:
        :param page_size:
        :param page_num:
        :param nested_handler:
        :param to_list:
        :return:
        """
        gauge = None
        label_keys = None
        for instance in self.es_pager_generator(req, page_size, page_num, to_list):
            if gauge is None:
                label_keys = self.label_keys(instance, nested_handler)
                gauge = GaugeMetricFamily(name, desc, labels=label_keys)
            gauge.add_metric(labels=self.label_values(instance, label_keys, nested_handler), value=1.0)
        return gauge

    def es_pager_generator(self, req, page_size, page_num, to_list):
        """
        为了适配新版本sdk
        :param req:
        :param page_size:
        :param page_num:
        :param to_list:
        :return:
        """
        req.set_size(page_size)
        while True:
            req.set_page(page_num)
            resp = self.client.do_action_with_exception(req)
            data = json.loads(resp)
            instances = to_list(data)
            for instance in instances:
                yield instance
            if len(instances) < page_size:
                break
            page_num += 1


    def label_keys(self, instance, nested_handler=None):
        if nested_handler is None:
            nested_handler = {}
        return [k for k, v in instance.items()
                if k in nested_handler or isinstance(v, str) or isinstance(v, int)]

    def label_values(self, instance, label_keys, nested_handler=None):
        if nested_handler is None:
            nested_handler = {}
        return map(lambda k: str(nested_handler[k](instance[k])) if k in nested_handler else try_or_else(
            lambda: str(instance[k]), ''),
                   label_keys)
