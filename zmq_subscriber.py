#!/usr/bin/env python3.5

import time, datetime
import copy
from collections import OrderedDict
from pprint import pprint
import zmq
import redis
import random
import configparser
import argparse
import os
import sys
import json
import geoip2.database

configfile = os.path.join(os.environ['DASH_CONFIG'], 'config.cfg')
cfg = configparser.ConfigParser()
cfg.read(configfile)

ONE_DAY = 60*60*24
ZMQ_URL = cfg.get('RedisGlobal', 'zmq_url')
CHANNEL = cfg.get('RedisLog', 'channel')
CHANNEL_LASTCONTRIB = cfg.get('RedisLog', 'channelLastContributor')
CHANNELDISP = cfg.get('RedisMap', 'channelDisp')
CHANNEL_PROC = cfg.get('RedisMap', 'channelProc')
PATH_TO_DB = cfg.get('RedisMap', 'pathMaxMindDB')

DEFAULT_PNTS_REWARD = cfg.get('CONTRIB', 'default_pnts_per_contribution')
categories_in_datatable = json.loads(cfg.get('CONTRIB', 'categories_in_datatable'))
DICO_PNTS_REWARD = {}
temp = json.loads(cfg.get('CONTRIB', 'pnts_per_contribution'))
for categ, pnts in temp:
    DICO_PNTS_REWARD[categ] = pnts
MAX_NUMBER_OF_LAST_CONTRIBUTOR = cfg.getint('CONTRIB', 'max_number_of_last_contributor')

serv_log = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisLog', 'db'))
serv_coord = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisMap', 'db'))
serv_redis_db = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisDB', 'db'))

reader = geoip2.database.Reader(PATH_TO_DB)

def getDateStrFormat(date):
    return str(date.year)+str(date.month).zfill(2)+str(date.day).zfill(2)

def publish_log(zmq_name, name, content, channel=CHANNEL):
    to_send = { 'name': name, 'log': json.dumps(content), 'zmqName': zmq_name }
    serv_log.publish(channel, json.dumps(to_send))

def push_to_redis_zset(keyCateg, toAdd, endSubkey="", count=1):
    now = datetime.datetime.now()
    today_str = getDateStrFormat(now)
    keyname = "{}:{}{}".format(keyCateg, today_str, endSubkey)
    serv_redis_db.zincrby(keyname, toAdd, count)

def push_to_redis_geo(keyCateg, lon, lat, content):
    now = datetime.datetime.now()
    today_str = getDateStrFormat(now)
    keyname = "{}:{}".format(keyCateg, today_str)
    serv_redis_db.geoadd(keyname, lon, lat, content)

def ip_to_coord(ip):
    resp = reader.city(ip)
    lat = float(resp.location.latitude)
    lon = float(resp.location.longitude)
    # 0.0001 correspond to ~10m
    # Cast the float so that it has the correct float format
    lat_corrected = float("{:.4f}".format(lat))
    lon_corrected = float("{:.4f}".format(lon))
    return { 'coord': {'lat': lat_corrected, 'lon': lon_corrected}, 'full_rep': resp }

def getCoordAndPublish(zmq_name, supposed_ip, categ):
    try:
        rep = ip_to_coord(supposed_ip)
        coord = rep['coord']
        coord_dic = {'lat': coord['lat'], 'lon': coord['lon']}
        ordDic = OrderedDict()
        ordDic['lat'] = coord_dic['lat']
        ordDic['lon'] = coord_dic['lon']
        coord_list = [coord['lat'], coord['lon']]
        push_to_redis_zset('GEO_COORD', json.dumps(ordDic))
        push_to_redis_zset('GEO_COUNTRY', rep['full_rep'].country.iso_code)
        push_to_redis_geo('GEO_RAD', coord['lon'], coord['lat'], json.dumps({ 'categ': categ, 'value': supposed_ip }))
        to_send = {
                "coord": coord,
                "categ": categ,
                "value": supposed_ip,
                "country": rep['full_rep'].country.name,
                "specifName": rep['full_rep'].subdivisions.most_specific.name,
                "cityName": rep['full_rep'].city.name,
                "regionCode": rep['full_rep'].country.iso_code,
                }
        serv_coord.publish(CHANNELDISP, json.dumps(to_send))
    except ValueError:
        print("can't resolve ip")
    except geoip2.errors.AddressNotFoundError:
        print("Address not in Database")

def getFields(obj, fields):
    jsonWalker = fields.split('.')
    itemToExplore = obj
    lastName = ""
    try:
        for i in jsonWalker:
            itemToExplore = itemToExplore[i]
            lastName = i
        if type(itemToExplore) is list:
            return { 'name': lastName , 'data': itemToExplore }
        else:
            return itemToExplore
    except KeyError as e:
        return ""

def noSpaceLower(str):
    return str.lower().replace(' ', '_')

#pntMultiplier if one contribution rewards more than others. (e.g. shighting may gives more points than editing)
def handleContribution(zmq_name, org, categ, action, pntMultiplier=1):
    if action in ['edit']:
        pass
        #return #not a contribution?
    # is a valid contribution
    try:
        pnts_to_add = DICO_PNTS_REWARD[noSpaceLower(categ)]
    except KeyError:
        pnts_to_add = DEFAULT_PNTS_REWARD
    pnts_to_add *= pntMultiplier

    push_to_redis_zset('CONTRIB_DAY', org, count=pnts_to_add)
    #CONTRIB_CATEG retain the contribution per category, not the point earned in this categ
    push_to_redis_zset('CONTRIB_CATEG', org, count=DEFAULT_PNTS_REWARD, endSubkey=':'+noSpaceLower(categ))
    serv_redis_db.sadd('CONTRIB_ALL_ORG', org)

    now = datetime.datetime.now()
    nowSec = int(time.time())
    serv_redis_db.zadd('CONTRIB_LAST:'+getDateStrFormat(now), nowSec, org)
    serv_redis_db.expire('CONTRIB_LAST:'+getDateStrFormat(now), ONE_DAY) #expire after 1 day

    updateOrgRank(org, pnts_to_add, eventTime, eventClassification)

    publish_log(zmq_name, 'CONTRIBUTION', {'org': org, 'categ': categ, 'action': action, 'epoch': nowSec }, channel=CHANNEL_LASTCONTRIB)

def updateOrgRank(orgName, pnts_to_add, contribType, eventTime, isClassified):
    keyname = 'CONTRIB_ORG:{org}:{orgCateg}'
    #update total points
    serv_redis_db.set(keyname.format(org=orgName, orgCateg='points'), pnts_to_add)
    #update contribution Requirement
    heavilyCount = 10
    recentDays = 31
    regularlyDays = 7
    isRecent = True if (datetime.datetime.now() - eventTime).days > recentDays
    contrib = [] #[[contrib_level, contrib_ttl], [], ...]
    if contribType == 'sighting':
        #[contrib_level, contrib_ttl]
        contrib.append([1, ONE_DAY*365]])
    if contribType == 'attribute' or contribType == 'object':
        contrib.append([2, ONE_DAY*365])
    if contribType == 'proposal' or contribType == 'discussion':
        contrib.append([3, ONE_DAY*365])
    if contribType == 'sighting' and isRecent:
        contrib.append([4, ONE_DAY*recentDays])
    if contribType == 'proposal' and isRecent:
        contrib.append([5, ONE_DAY*recentDays])
    if contribType == 'event':
        contrib.append([6, ONE_DAY*365])
    if contribType == 'event':
        contrib.append([7, ONE_DAY*recentDays])
    if contribType == 'event':
        contrib.append([8, ONE_DAY*regularlyDays])
    if contribType == 'event' and isClassified:
        contrib.append([9, ONE_DAY*regularlyDays])
    if contribType == 'sighting' and sightingWeekCount>heavilyCount:
        contrib.append([10, ONE_DAY*regularlyDays])
    if (contribType == 'attribute' or contribType == 'object') and attributeWeekCount>heavilyCount:
        contrib.append([11, ONE_DAY*regularlyDays])
    if contribType == 'proposal' and proposalWeekCount>heavilyCount:
        contrib.append([12, ONE_DAY*regularlyDays])
    if contribType == 'event' and eventWeekCount>heavilyCount:
        contrib.append([13, ONE_DAY*regularlyDays])
    if contribType == 'event' and eventWeekCount>heavilyCount  and isClassified:
        contrib.append([14, ONE_DAY*regularlyDays])

    for rankReq, ttl:
        serv_redis_db.set(keyname.format(org=orgName, orgCateg='CONTRIB_REQ_'+str(rankReq)), 1)
        serv_redis_db.expire(keyname.format(org=orgName, orgCateg='CONTRIB_REQ_'+str(i)), ttl)


##############
## HANDLERS ##
##############

def handler_log(zmq_name, jsonevent):
    print('sending', 'log')
    return

def handler_dispatcher(zmq_name, jsonObj):
    if "Event" in jsonObj:
        handler_event(zmq_name, jsonObj)

def handler_keepalive(zmq_name, jsonevent):
    print('sending', 'keepalive')
    to_push = [ jsonevent['uptime'] ]
    publish_log(zmq_name, 'Keepalive', to_push)

def handler_sighting(zmq_name, jsonsight):
    print('sending' ,'sighting')
    org = jsonsight['org']
    categ = jsonsight['categ']
    action = jsonsight['action']
    handleContribution(zmq_name, org, categ, action, pntMultiplier=2)
    return

def handler_event(zmq_name, jsonobj):
    #fields: threat_level_id, id, info
    jsonevent = jsonobj['Event']
    #redirect to handler_attribute
    if 'Attribute' in jsonevent:
        attributes = jsonevent['Attribute']
        if type(attributes) is list:
            for attr in attributes:
                jsoncopy = copy.deepcopy(jsonobj)
                jsoncopy['Attribute'] = attr
                handler_attribute(zmq_name, jsoncopy)
        else:
            handler_attribute(zmq_name, attributes)

def handler_attribute(zmq_name, jsonobj):
    # check if jsonattr is an attribute object
    if 'Attribute' in jsonobj:
        jsonattr = jsonobj['Attribute']

    to_push = []
    for field in json.loads(cfg.get('Log', 'fieldname_order')):
        if type(field) is list:
            to_join = []
            for subField in field:
                to_join.append(getFields(jsonobj, subField))
            to_add = cfg.get('Log', 'char_separator').join(to_join)
        else:
            to_add = getFields(jsonobj, field)
        to_push.append(to_add)

    #try to get coord from ip
    if jsonattr['category'] == "Network activity":
        getCoordAndPublish(zmq_name, jsonattr['value'], jsonattr['category'])

    handleContribution(zmq_name, jsonobj['Event']['Orgc']['name'], jsonattr['category'], jsonobj['action'])
    # Push to log
    publish_log(zmq_name, 'Attribute', to_push)


def process_log(zmq_name, event):
    event = event.decode('utf8')
    topic, eventdata = event.split(' ', maxsplit=1)
    jsonevent = json.loads(eventdata)
    dico_action[topic](zmq_name, jsonevent)


def main(zmqName):
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(ZMQ_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, '')

    while True:
        content = socket.recv()
        content.replace(b'\n', b'') # remove \n...
        zmq_name = zmqName
        process_log(zmq_name, content)


dico_action = {
        "misp_json":                handler_dispatcher,
        "misp_json_event":          handler_event,
        "misp_json_self":           handler_keepalive,
        "misp_json_attribute":      handler_attribute,
        "misp_json_sighting":       handler_sighting,
        "misp_json_organisation":   handler_log,
        "misp_json_user":           handler_log,
        "misp_json_conversation":   handler_log
        }


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='A zmq subscriber. It subscribe to a ZNQ then redispatch it to the misp-dashboard')
    parser.add_argument('-n', '--name', required=False, dest='zmqname', help='The ZMQ feed name', default="MISP Standard ZMQ")
    parser.add_argument('-u', '--url', required=False, dest='zmqurl', help='The URL to connect to', default=ZMQ_URL)
    args = parser.parse_args()

    main(args.zmqname)
    reader.close()
