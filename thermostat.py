try:
    import googleclouddebugger
    googleclouddebugger.enable(breakpoint_enable_canary=False)

except ImportError:
    pass



import os
import logging
from flask import Flask, url_for, request
import json
from google.cloud import pubsub_v1
import time
from datetime import datetime
from google.cloud import secretmanager
from google.cloud import storage
from google.oauth2 import id_token
import google.auth
from google.auth.transport.requests import Request
import base64
import copy
import requests
import pandas as pd
from pytz import timezone
import pytz
import iso8601
import re
import numpy as np

from accumulator import Accumulator

from utils import utcnow, ceil_dt, get_tz, get_utc_tz
from yadt import scan_and_apply_tz, utc_to_toronto, apply_tz_toronto, parse_date

from thermostat_iot_control import thermostat_iot_control
from thermostat_decision import heating_decision


# Instantiates a client
storage_client = storage.Client()

# The name for the new bucket
bucket_name = "thermostat_metric_data"
bucket = storage_client.bucket(bucket_name)


project_id = os.environ['PROJECT_ID']

FORMAT_DATE_SEP = "%Y-%m-%d %H:%M:%S"
FORMAT_DATE = "%Y%m%d %H%M%S"
FORMAT_DATE_DASH = "%Y%m%d-%H%M%S"

app = Flask(__name__)
app.config["DEBUG"] = True
app.register_blueprint(thermostat_iot_control, url_prefix="/iot")

if __name__ != '__main__':
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)


def create_file(payload, filename):
    """Create a file.

  The retry_params specified in the open call will override the default
  retry params for this particular file handle.

  Args:
    filename: filename.
  """
    blob = bucket.blob(filename)

    blob.upload_from_string(data=payload,
                            content_type='text/plain')

def get_metric_list_from_bucket():
    blobs = list(storage_client.list_blobs(bucket_name, prefix='thermostat'))
    metric_list = []
    for _b in blobs:
        filename = ' '.join(_b.name.rsplit('-', 2)[1:3])
        try:
            dateobj = datetime.strptime(filename,FORMAT_DATE)
            item = {
                "name": _b.name,
                "dateobj": dateobj
            }
            metric_list.append(item)
        except ValueError:
            pass


    return metric_list

def get_metric_from_bucket(last=0, pref='thermostat', last_file=None, first_file=None):

    blobs = list(storage_client.list_blobs(bucket_name, prefix=pref))
    if last_file != None:
        i = 0
        for b in blobs:
            if blobs[i].name == last_file:
                break
            i = i + 1

        if blobs[i].name == last_file:
            last = i


    if last != 1:
        last = range(-1,0-int(last)-1,-1)
    else:
        last = range(-1,-2,-1)
    last_json = []
    print("Get last {} thermostat metric(s).".format(last))
    for i in last:
        try:
            json_str = blobs[i].download_as_string()
            j = json.loads(json_str)
            j = scan_and_apply_tz(j)

            j["file"] = blobs[i].name
            j['datetime'] = scan_and_apply_tz(datetime.fromtimestamp(
                                                     j['timestamp']))
            j['dateobj'] = apply_tz_toronto(datetime.fromtimestamp(j['timestamp']))

            last_json.append(j)
        except NameError:
            print("Payload is not JSON.")
        except json.JSONDecodeError:
            print("JSON error")
            print(json_str)

    return last_json

@app.route('/metric/thermostat/', methods=['GET'])
def get_metric_thermostat():

    last = request.args.get('last', 1)

    last_json = get_metric_from_bucket(last)
    last_json = json.dumps(last_json)


    return last_json


@app.route('/metric/thermostat/', methods=['POST'])
def store_metric_thermostat():
    envelope = request.get_json()
    if not envelope:
        msg = 'no Pub/Sub message received'
        print(f'error: {msg}')
        return f'Bad Request: {msg}', 400

    if not isinstance(envelope, dict) or 'message' not in envelope:
        msg = 'invalid Pub/Sub message format'
        print(f'error: {msg}')
        return f'Bad Request: {msg}', 400

    pubsub_message = envelope['message']

    payload = ''
    if isinstance(pubsub_message, dict) and 'data' in pubsub_message:
        payload = base64.b64decode(pubsub_message['data']).decode('utf-8').strip()

    filename = "thermostat-" + datetime.now().strftime(FORMAT_DATE_DASH)
    create_file(payload, filename)

    return ('', 204)


@app.route('/metric/environment-sensor/', methods=['GET'])
def get_metric_environment():

    last = request.args.get('last', 1)

    last_json = get_metric_from_bucket(last=last,
                                       pref="environment_sensor_basement-")
    last_json = json.dumps(last_json)

    return last_json

# device_id:environment-sensor; location:house.basement; temperature:21.69;
@app.route('/metric/environment-sensor/', methods=['POST'])
def store_metric_environment():
    envelope = request.get_json()
    if not envelope:
        msg = 'no Pub/Sub message received'
        print(f'error: {msg}')
        return f'Bad Request: {msg}', 400

    if not isinstance(envelope, dict) or 'message' not in envelope:
        msg = 'invalid Pub/Sub message format'
        print(f'error: {msg}')
        return f'Bad Request: {msg}', 400

    pubsub_message = envelope['message']

    payload = ''
    if isinstance(pubsub_message, dict) and 'data' in pubsub_message:
        payload = base64.b64decode(
            pubsub_message['data']).decode('utf-8').strip()

    if "location:house.basement" in payload:
        print(re.match("temperature\:([0-9]+\.[0-9]+)", payload))
        json_content = {"temperature": float(re.match(".+temperature:([0-9]+\.[0-9]+)", payload).groups()[0]),
                        "original_payload": payload}
        filename = "environment_sensor_basement-" + datetime.now().strftime(FORMAT_DATE_DASH)
        create_file(json.dumps(json_content), filename)

        accumulator = Accumulator(app.logger)
        n = utcnow()
        try:
            accumulator.add_temperature(n, temp_basement=json_content.get('temperature'))
        except ValueError as ex:
            app.logger.warn("Accumulator - no value to add - content: {} --- {}".format(payload,ex))

    return ('', 204)


def has_no_empty_params(rule):
    defaults = rule.defaults if rule.defaults is not None else ()
    arguments = rule.arguments if rule.arguments is not None else ()
    return len(defaults) >= len(arguments)


@app.route("/site-map")
def site_map():
    links = []
    for rule in app.url_map.iter_rules():
        # Filter out rules we can't navigate to in a browser
        # and rules that require parameters
        if ("GET" in rule.methods or "POST" in rule.methods) and has_no_empty_params(rule):
            url = url_for(rule.endpoint, **(rule.defaults or {}))
            links.append((url, rule.endpoint))
    # links is now a list of url, endpoint tuples

    return (str(links))


def resample_disturbances(data):
    ind = []
    for d in data:
        ind.append(datetime.strptime(d["dt"],FORMAT_DATE_SEP))
    df = pd.DataFrame(data, index=ind)
    df = df.resample('15Min').interpolate(method='linear')
    df['dt'] = df.index.values
    data2 = df.replace({np.nan: None}).to_dict('records')
    for d in data2:
        d['dt'] = d['dt'].to_pydatetime().strftime(FORMAT_DATE_SEP)
    #data2['dt'] = data2['dt'].to_pydatetime().strftime("%Y-%m-%d %H:%M:%S")
    return data2


url_weather = 'https://climacell-agent-ppb6otnevq-uk.a.run.app'

def query(url_query, audience, method='GET', body=None):
    open_id_connect_token = id_token.fetch_id_token(Request(),
                                                            audience=audience)

    resp = requests.request(
        method,
        url_query,
        headers={'Authorization': 'Bearer {}'.format(open_id_connect_token)},
        json=body)


    try:
        resp.json()
    except:
        app.logger.error("Error while querying : {} - {}".format(
            url_query, resp.reason))
        pass

    return resp


def get_weather_realtime(last=1, realtime_start=None, realtime_end=None):
    url_query=None
    if (realtime_start != None and realtime_end != None):
        url_query = url_weather + '/store/realtime/?start={}&end={}'.format(
            realtime_start,realtime_end)
    else:
        url_query = url_weather + '/store/realtime/?last=' + str(last)

    resp = query(url_query, url_weather)

    return resp.json()


def get_weather_hourly(last=1, hourly_start=None, hourly_end=None):
    url_query = None
    if(hourly_start != None and hourly_end != None):
        url_query = url_weather + '/store/hourly/?start={}&end={}'.format(
            hourly_start, hourly_end)
    else:
        url_query = url_weather + '/store/hourly/?last=' + str(last)

    resp = query(url_query, url_weather)

    return resp.json()

def get_set_point(date):
    if date.hour >= 21 or (date.hour <= 5 and date.minute >= 30):
        return 18
    else:
        return 22

def round_date(date):
    minute = 15 * round((float(date.minute) + float(date.second) / 60) / 15)
    if minute == 60:
        minute = 0
    date = datetime(date.year, date.month, date.day, date.hour, minute)
    return date

def map_climacell_data(data):
    date = parse_date(data['observation_time']['value'],toronto=True)
    date = round_date(date)
    return {
        # TODO : Dt should be NOW
        "dt": date.strftime(FORMAT_DATE_SEP),
        "Indoor Temp. Setpoint": get_set_point(date),
        "Outdoor Temp.": data['temp']['value'],
        "Outdoor RH": data['humidity']['value'],
        "Wind Speed": data['wind_speed']['value'],
        "Wind Direction": data['wind_direction']['value'],
        "Direct Solar Rad.": data['surface_shortwave_radiation']['value'],
        "name": data['name']
    }

def format_date(date):
    date = date.strftime(FORMAT_DATE_SEP)
    return date

@app.route("/digest")
def digest():
    hourly_start = request.args.get('hourly_start', None)
    hourly_end = request.args.get('hourly_end', None)
    realtime_start = request.args.get('realtime_start', None)
    realtime_end = request.args.get('realtime_end', None)

    return digest(hourly_start,
                    hourly_end,
                    realtime_start,
                    realtime_end)

def coil_power(stove_exhaust_temp):
    max_coil_power = 10613.943465
    min_coil_power = 0.0
    max_stove_exhaust_temp = 130.0

    if stove_exhaust_temp > 30:
        coil_power = (stove_exhaust_temp * max_coil_power) / max_stove_exhaust_temp
    else:
        coil_power = 0

    return coil_power


def digest(

        hourly_start=None,  #TODO remove
        hourly_end=None,  #TODO remove
        realtime_start=None,  #TODO remove
        realtime_end=None,
        hourly_last=1,
        realtime_last=14,):

    therm_acc = get_accumulate().to_df()
    hourly = get_weather_hourly(last=hourly_last)
    realtime = get_weather_realtime(last=realtime_last)
    therm_acc['datetime'] = therm_acc.index
    x_current_thermostat = therm_acc.tail(1)
    app.logger.debug("temp_basement : {}".format(x_current_thermostat.iloc[0].get('temp_basement', default="not available")))
    current_realtime = realtime.pop(0)
    date_t = pd.to_datetime(x_current_thermostat.iloc[0]['datetime'])
    date_t = round_date(date_t)
    indoor_setpoint = get_set_point(date_t)
    app.logger.info("Next Action Setpoint : {}".format(indoor_setpoint))
    result = {"digest": {}}
    result["digest"]["current"] = {
        "Htg SP": 22,
        "Indoor Temp. Setpoint": indoor_setpoint,
        "Occupancy Flag": bool(x_current_thermostat.iloc[0].get('motion', default=False)),
        "PPD": 99,
        "Coil Power": coil_power(x_current_thermostat.iloc[0]['stove_exhaust_temp']),
        "MA Temp.": 18,
        "Sys Out Temp.": x_current_thermostat.iloc[0].get('temp_basement'),
        "dt": format_date(date_t),
        "Outdoor Temp.": current_realtime['temp']['value'],
        "Outdoor RH": current_realtime['humidity']['value'],
        "Wind Speed": current_realtime['wind_speed']['value'],
        "Wind Direction": current_realtime['wind_direction']['value'],
        "Direct Solar Rad.": current_realtime['surface_shortwave_radiation']['value'] or 0.0,
        "Indoor Temp.": x_current_thermostat.iloc[0]['temperature']
    }
    result["digest"]["date"] = format_date(date_t)
    disturbances = []

    for r in realtime:
        date_r = datetime.strptime(r['observation_time']['value'],
                                   '%Y-%m-%dT%H:%M:%S.%f%z')
        date_r = date_r.replace(tzinfo=None)
        if date_r < date_t:
            mapping = map_climacell_data(r)
            date_r_pd = pd.to_datetime(date_r)
            nearest_t = therm_acc.iloc[therm_acc.index.get_loc(
                date_r_pd, method='nearest')]
            mapping["Indoor Temp."] = nearest_t["temperature"]
            if nearest_t["motion"]:
                mapping["Occupancy Flag"] = 1
            else:
                mapping["Occupancy Flag"] = 0

            disturbances.append(copy.deepcopy(mapping))

    for h in hourly:
        date_h = parse_date(h['observation_time']['value'])
        #date_h = date_h.replace(tzinfo=None)
        date_temp = parse_date(result["digest"]["date"], toronto=True)
        diff = ((date_h - date_temp).total_seconds() // 3600)
        if diff < 4 and diff >= 0:
            mapping = map_climacell_data(h)
            mapping["Occupancy Flag"] = 0

            disturbances.append(copy.deepcopy(mapping))

    disturbances = resample_disturbances(disturbances)
    result["digest"]["disturbances"] = disturbances
    return result["digest"]


url_gnu_rl = "https://gnu-rl-agent-ppb6otnevq-uk.a.run.app"
#url_gnu_rl = "http://127.0.0.1:5001"


@app.route("/next-action")
def next_action():
    hourly_start = request.args.get('hourly_start', None)
    hourly_end = request.args.get('hourly_end', None)
    realtime_start = request.args.get('realtime_start', None)
    realtime_end = request.args.get('realtime_end', None)


    body = digest(hourly_start, hourly_end, realtime_start, realtime_end)
    url_query = url_gnu_rl + '/mpc/'
    resp = query(url_query, url_gnu_rl, 'POST', body)


    accumulator = Accumulator(app.logger)

    mpc_dict = resp.json().copy()
    for k in list(mpc_dict.keys()):
        mpc_dict['mpc_' + k] = mpc_dict.pop(k)

    current_dict = body['current'].copy()
    current_dict['dt'] = parse_date(current_dict['dt'], toronto=True)
    n = current_dict['dt']
    current_dict['dt'] = current_dict['dt'].astimezone(get_utc_tz())
    current_dict['dt'] = current_dict['dt'].timestamp()
    current_dict['dt_utc'] = current_dict['dt']
    del current_dict['dt']

    for k in list(current_dict.keys()):
        current_dict['current_' + k.replace(" ", "_")
                                  .replace(".", "")
                                  .lower()] = current_dict.pop(k)

    try:
        accumulator.add_temperature2(n, value_dict=current_dict)
        accumulator.add_temperature2(n, value_dict=mpc_dict)

    except ValueError as ex:
        app.logger.warn(
            "Accumulator - no value to add - content: {} --- {}".format(
                mpc_dict, ex))

       

    app.logger.info("Next Action Result : {}".format(resp.json()))
    app.logger.info("NextAction_Setpoint:{}".format(
        resp.json()['sat_stpt']))

    next_action_result = {
        "mpc": resp.json(),
        "heating_decision": heating_decision(resp.json())
    }  


    return next_action_result


@app.route('/accumulate/', methods=['POST'])
def test_accumulate():
    j = request.get_json()
    accumulator = acc(j)

    resp = accumulator.to_dict()
    print(resp)
    return resp


def acc(j):
    accumulator = Accumulator(app.logger)
    n = utcnow()

    if j.get('temperature') is not None:
        j['temperature'] = float(j.get('temperature'))
    if j.get('humidity') is not None:
        j['humidity'] = float(j.get('humidity'))
    if j.get('stove_exhaust_temp') is not None:
        j['stove_exhaust_temp'] = float(j.get('stove_exhaust_temp'))


    try:
        accumulator.add_temperature2(n, value_dict=j)
    except ValueError as ex:
        app.logger.warn(
            "Accumulator - no value to add - content: {} --- {}".format(
                payload, ex))

    return accumulator

@app.route('/metric/accumulate/', methods=['POST'])
def accumulate_metric_thermostat():
    envelope = request.get_json()
    if not envelope:
        msg = 'no Pub/Sub message received'
        print(f'error: {msg}')
        return f'Bad Request: {msg}', 400

    if not isinstance(envelope, dict) or 'message' not in envelope:
        msg = 'invalid Pub/Sub message format'
        print(f'error: {msg}')
        return f'Bad Request: {msg}', 400

    pubsub_message = envelope['message']

    payload = ''
    if isinstance(pubsub_message, dict) and 'data' in pubsub_message:
        payload = base64.b64decode(
            pubsub_message['data']).decode('utf-8').strip()
    try:
        j = json.loads(payload)

        acc(json.loads(payload))
    except Exception as ex:
        app.logger.error("Unable to loads payload Json {} : {}".format(
            ex,payload))

    return ('', 204)


@app.route('/metric/accumulate/', methods=['GET'])
def get_accumulate_metric_thermostat():

    load = int(request.args.get('load', None))
    records = bool(request.args.get('records', False))

    if load>=1:
        accumulate = get_accumulate(load)
    else:
        accumulate = get_accumulate()

    if records:
        resp = accumulate.to_json_records()
    else:
        resp = accumulate.to_dict()

    return (resp, 200)


def get_accumulate(load=2):
    accumulator = Accumulator(app.logger)
    accumulator.load(load,hold=True)

    return accumulator




if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))