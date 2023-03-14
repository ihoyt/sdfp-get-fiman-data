import os
import sys
import time
from unicodedata import numeric
from pytz import timezone
import requests
from datetime import datetime
from datetime import timedelta
import pandas as pd
from io import StringIO
from urllib.request import urlopen
import xmltodict
import numpy as np
import warnings
from sqlalchemy import create_engine
import inspect
import traceback

########################
# Utility functions    #
########################

# override print so each statement is timestamped
old_print = print
def timestamped_print(*args, **kwargs):
  old_print(datetime.now(), *args, **kwargs)
print = timestamped_print


def slicer(my_str,sub):
        index=my_str.find(sub)
        if index !=-1 :
            return my_str[index:] 
        else :
            raise Exception('Sub string not found!')
        
        
def postgres_upsert(table, conn, keys, data_iter):
    from sqlalchemy.dialects.postgresql import insert

    data = [dict(zip(keys, row)) for row in data_iter]

    insert_statement = insert(table.table).values(data)
    upsert_statement = insert_statement.on_conflict_do_update(
        constraint=f"{table.table.name}_pkey",
        set_={c.key: c for c in insert_statement.excluded},
    )
    conn.execute(upsert_statement)

#############################
# Method-specific functions #
#############################

def get_fiman_atm(id, sensor, begin_date, end_date):
    """Retrieve data from specified sensor from the NOAA tides and currents API

    Args:
        id (str): Station id
        sensor (str): Requested sensor at station
        begin_date (str): Beginning date of requested time period. Format: %Y%m%d %H:%M
        end_date (str): End date of requested time period. Format: %Y%m%d %H:%M
        
    Returns:
        r_df (pd.DataFrame): DataFrame of requested data from specified station and time range. Dates in UTC
    """    
    print(inspect.stack()[0][3])    # print the name of the function we just entered
    
    #
    # It looks like if the data are not long enough (date-wise), the query to fiman will not return anything
    # at which point this will fail.
    #

    fiman_gauge_keys = pd.read_csv("data/fiman_gauge_key.csv").query("site_id == @id & Sensor == @sensor")
    
    new_begin_date = pd.to_datetime(begin_date, utc=True) - timedelta(seconds = 3600)
    new_end_date = pd.to_datetime(end_date, utc=True) + timedelta(seconds = 3600)
    
    query = {'site_id' : fiman_gauge_keys.iloc[0]["site_id"],
             'data_start' : new_begin_date.strftime('%Y-%m-%d %H:%M:%S'),
             'end_date' : new_end_date.strftime('%Y-%m-%d %H:%M:%S'),
             'format_datetime' : '%Y-%m-%d %H:%M:%S',
             'tz' : 'utc',
             'show_raw' : True,
             'show_quality' : True,
             'sensor_id' : fiman_gauge_keys.iloc[0]["sensor_id"]}
    print(query)    # FOR DEBUGGING
    
    # try:
    r = requests.get(os.environ.get("FIMAN_URL"), params=query, timeout=120)
    # except requests.exceptions.Timeout:
    #     return pd.DataFrame()

    j = r.content
    print(j)
    doc = xmltodict.parse(j)
    
    unnested = doc["onerain"]["response"]["general"]["row"]
    
    r_df = pd.DataFrame.from_dict(unnested)

    r_df["date"] = pd.to_datetime(r_df["data_time"], utc=True); 
    r_df["id"] = str(id); 
    r_df["notes"] = "FIMAN"
    r_df["type"] = "water_level" if sensor == "Water Elevation" else "pressure"
    r_df = r_df.loc[:,["id","date","data_value","notes", "type"]].rename(columns = {"data_value":"value", "notes": "api_name"})

    return r_df.drop_duplicates(subset=['id', 'date'])

#####################
# atm API functions #
#####################

def get_atm_pressure(atm_id, atm_src, begin_date, end_date):
    """Yo, yo, yo, it's a wrapper function!

    Args:
        atm_id (str): Value from `sensor_surveys` table that declares the ID of the station to use for atmospheric pressure data.
        atm_src (str): Value from `sensor_surveys` table that declares the source of the atmospheric pressure data.
        begin_date (str): The beginning date to retrieve data. Format: %Y%m%d %H:%M
        end_date (str): The end date to retrieve data. Format: %Y%m%d %H:%M

    Returns:
        pandas.DataFrame: Atmospheric pressure data for the specified time range and source
    """    
    print(inspect.stack()[0][3])    # print the name of the function we just entered

    match atm_src.upper():
        case "NOAA":
            return get_noaa_atm(id = atm_id, begin_date = begin_date, end_date = end_date)
        case "NWS":
            return get_nws_atm(id = atm_id, begin_date = begin_date, end_date = end_date)
        case "ISU":
            return get_isu_atm(id = atm_id, begin_date = begin_date, end_date = end_date)
        case "FIMAN":
            return get_fiman_atm(id = atm_id, begin_date = begin_date, end_date = end_date)
        case _:
            return "No valid `atm_src` provided! Make sure you are supplying a string"

def main():
    print("Entering main of process_pressure.py")
    
    # from env_vars import set_env_vars
    # set_env_vars()
    
    ########################
    # Establish DB engine  #
    ########################

    SQLALCHEMY_DATABASE_URL = "postgresql://" + os.environ.get('POSTGRESQL_USER') + ":" + os.environ.get(
        'POSTGRESQL_PASSWORD') + "@" + os.environ.get('POSTGRESQL_HOSTNAME') + "/" + os.environ.get('POSTGRESQL_DATABASE')

    engine = create_engine(SQLALCHEMY_DATABASE_URL)
    
    #####################
    # Collect new data  #
    #####################

    end_date = pd.to_datetime(datetime.utcnow())
    start_date = end_date - timedelta(days=int(os.environ.get('NUM_DAYS')))

    # Get water level data

    stations = pd.read_sql_query("SELECT DISTINCT wl_id FROM sensor_surveys WHERE wl_src='FIMAN'", engine)
    stations = stations.to_numpy()
    
    for wl_id in stations:
        print("Querying site " + wl_id[0] + "...")
        new_data = get_fiman_atm(wl_id[0], 'Water Elevation', start_date, end_date)

        if new_data.shape[0] == 0:
            warnings.warn("- No new raw data!")
            return
        
        print(new_data.shape[0] , "new records!")
        print(new_data.head(1000))
        
        new_data.to_sql("external_api_data", engine, if_exists = "append", method=postgres_upsert, index=False)
        time.sleep(10)

    # Get atm_pressure data
    stations = pd.read_sql_query("SELECT DISTINCT atm_station_id FROM sensor_surveys WHERE atm_data_src='FIMAN'", engine)
    stations = stations.to_numpy()

    for atm_station_id in stations:
        print("Querying site " + atm_station_id[0] + "...")
        new_data = get_fiman_atm(atm_station_id[0], 'Barometric Pressure', start_date, end_date)

        if new_data.shape[0] == 0:
            warnings.warn("- No new raw data!")
            return
        
        print(new_data.shape[0] , "new records!")
        
        new_data.to_sql("external_api_data", engine, if_exists = "append", method=postgres_upsert, index=False)
        time.sleep(10)
    
    engine.dispose()

if __name__ == "__main__":
    main()
