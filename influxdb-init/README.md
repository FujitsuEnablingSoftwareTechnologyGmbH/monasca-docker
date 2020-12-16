# Influx Init

## Variables
INFLUXDB_DEFAULT_RETENTION - determines how long the data should be stored
before it is dropped, see [retention policies][1] for more detail.
Acceptable values are [durations][2] and `INF` for infinite (Default: `INF`)

INFLUXDB_SHARD_DURATION - determines the size of data shards. An entire shard
is dropped once all of the data it contains is out of the retention period.
Default values are explained in [retention policies][1] and acceptable values
are [durations][2]

[1]: https://docs.influxdata.com/influxdb/v1.3/query_language/database_management/#retention-policy-management
[2]: https://docs.influxdata.com/influxdb/v1.3/query_language/spec/#durations

## Migration To new Indexing Structure TSI

### Background

In previous versions, all indexing information for time series has been held in memory.
Disadvantage of this solution: 
* Huge amount of RAM needed
* There's an upper bound of series that can be held on a machine  

With influxdb 1.7, a new indexing structure has been introduced: **TSI: Time Series Index**  

**Advantages of TSI:**
* Storage of indexing information on disk  
* Possible data size not restricted by RAM  

monasca is using influxdb 1.8. 
New indexing structure will be enabled automatically when installing monasca.  
However, existing data won't be migrated automatically.

Thus, there are 2 options for existing data:
* Migrate existing data. Thus, new indexing structure will be used for all data from the beginning.  
* Do not migrate existing data.    
  In this scenario, shards newly created will use TSI indexing structure.    
  For existing data, still the old, "in-memory"-index will be used.  
  Over time, old data will be removed (based on retention policy).  
  After a complete cycle of retention, no more "old" data with old indexing structure will exist any longer. 
  
We recommend data migration especially if:
* influxdb has a huge size (many series: 100,000 or more)
* memory issues are observed on machine where influxdb has been installed

The next chapter describes migration to new indexing structure TSI for existing databases.

### Migrate existing data to new indexing structure TSI

**Background: Shards**  

Data in influxdb is organized in shards.  
In monasca, shards are configured to store 24 hrs of data.  
Depending on the defined retention policy (default for monasca: 60d), data will be deleted if they expire.  
Only complete shards will be deleted. 

The indexing structure is defined for each shard.  
If the new indexing structure (TSI) shall be used, shards that existed before ugrade to influxdb 1.8 need to be migrated.

**Migration Steps** 
 
Please execute the following steps:  
* Stop monasca server: `docker-compose ... stop`
* Start influxdb container: 
  `docker start <influx-container-id>`  
* Login to influxdb container: `docker exec –it \influx-container> /bin/sh`
* In the container, execute the following commands:
    * `cd /var/lib influxdb`
    * `influx_inspect buildtsi -datadir ./data -waldir ./wal`   
      Confirm question to execute migration as 'root' with 'y'  
   **Note**: Data migration can be done as well for single shards. For details, pls. refer to   
   https://docs.influxdata.com/influxdb/v1.8/tools/influx_inspect/#influx-inspect-utility*  
* Check output from `influx_inspect` :  
    * Only messages with "lvl=info" have been written
    * Last message should be: "Moving tsi to permanent location"
* Exit container, if migration completed successfully 
* Stop influxdb container: 
  `docker stop <influx-container-id>` 
* Start monasca server: `docker-compose ... up -d`
* If you want to check that influx is now really using tsi, pls. execute the following steps:  
    * Extract log for influxdb: `docker logs <influxdb container id> >& <path for output-file>`
    * Edit file, e.g.: `vi <path for output-file>`
    * Go to EOF 
    * Search last start of influxdb: search for last occurrence of string "InfluxDB starting"
    * Search for msg "index opened with ... partitions":  
      At the end of the line, index type is written: "tsi"
