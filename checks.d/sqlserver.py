'''
Check the performance counters from SQL Server
'''
# stdlib
import traceback

# project
from checks import AgentCheck

# 3rd party
import adodbapi

ALL_INSTANCES = 'ALL'
VALID_METRIC_TYPES = ('gauge', 'rate', 'histogram')

PERF_LARGE_RAW_BASE = 1073939712
PERF_RAW_LARGE_FRACTION = 537003264
PERF_AVERAGE_BULK = 1073874176
PERF_COUNTER_BULK_COUNT = 272696576
PERF_COUNTER_LARGE_RAWCOUNT = 65792

class SQLServer(AgentCheck):

    SOURCE_TYPE_NAME = 'sql server'

    METRICS = [
        ('sqlserver.buffer.cache_hit_ratio', 'Buffer cache hit ratio', None), # RAW_LARGE_FRACTION
        ('sqlserver.buffer.page_life_expectancy', 'Page life expectancy', None), # LARGE_RAWCOUNT
        ('sqlserver.stats.batch_requests', 'Batch Requests/sec', None), # BULK_COUNT
        ('sqlserver.stats.sql_compilations', 'SQL Compilations/sec', None), # BULK_COUNT
        ('sqlserver.stats.sql_recompilations', 'SQL Re-Compilations/sec', None), # BULK_COUNT
        ('sqlserver.stats.connections', 'User connections', None), # LARGE_RAWCOUNT
        ('sqlserver.stats.lock_waits', 'Lock Waits/sec', '_Total'), # BULK_COUNT
        ('sqlserver.access.page_splits', 'Page Splits/sec', None), # BULK_COUNT
        ('sqlserver.stats.procs_blocked', 'Processes Blocked', None), # LARGE_RAWCOUNT
        ('sqlserver.buffer.checkpoint_pages', 'Checkpoint pages/sec', None) #BULK_COUNT
    ]

    def __init__(self, name, init_config, agentConfig, instances = None):
        AgentCheck.__init__(self, name, init_config, agentConfig, instances)

        # Cache connections
        self.connections = {}

        self.instances_metrics = {}
        for instance in instances:

            # metrics_to_collect contains the metric to collect in the following
            # format:
            # (name in datadog, name in sql server, collector function to use for reporting,
            #  type of sql metric, instance_name, tag_by )
            metrics_to_collect = []
            for metric in METRICS:
                name, counter_name, instance_name = metric
                try:
                    sql_type = self.get_sql_type(instance, sql_name)
                    metrics_to_collect.append((name, counter_name,
                                               None, sql_type,
                                               instance_name, None))
                except Exception:
                    self.log.warning("Can't load the metric %s, ignoring", name)
                    continue

            # Load any custom metrics from conf.d/sqlserver.yaml
            for row in init_config.get('custom_metrics', []):
                type = row.get('type')
                if type is not None and type not in VALID_METRIC_TYPES:
                    self.log.error('%s has an invalid metric type: %s' \
                                    % (row['name'], type))
                sql_type = None
                try:
                    if type is None:
                        sql_type = self.get_sql_type(instance, row['counter_name'])
                except Exception:
                    self.log.warning("Can't load the metric %s, ignoring", name)
                    continue


                metrics_to_collect.append((row['name'], row['counter_name'],
                                           type, sql_tpye,
                                           row.get('instance_name', ''), row.get('tag_by', None)))

            instance_key = self._conn_key(instance)
            self.instances_metrics[instance_key] = metrics_to_collect


    def _conn_key(self, instance):
        ''' Return a key to use for the connection cache
        '''
        host = instance.get('host', '127.0.0.1;1433')
        username = instance.get('username')
        password = instance.get('password')
        database = instance.get('database', 'master')
        return '%s:%s:%s:%s' % (host, username, password, database)

    def _conn_string(self, host, username, password, database):
        ''' Return a connection string to use with adodbapi
        '''
        conn_str = 'Provider=SQLOLEDB;Data Source=%s;Initial Catalog=%s;' \
                        % (host, database)
        if username:
            conn_str += 'User ID=%s;' % (username)
        if password:
            conn_str += 'Password=%s;' % (password)
        if not username and not password:
            conn_str += 'Integrated Security=SSPI;'
        return conn_str

    def get_cursor(self, instance):
        conn_key = self._conn_key(instance)

        if conn_key not in self.connections:
            try:
                conn_str = self._conn_string(host, username, password, database)
                conn = adodbapi.connect(conn_str)
                self.connections[conn_key] = conn
            except Exception, e:
                cx = "%s - %s" % (host, database)
                raise Exception("Unable to connect to SQL Server for instance %s.\n %s" \
                    % (cx, traceback.format_exc()))

        conn = self.connections[conn_key]
        cursor = conn.cursor()

    def check(self, instance):
        tags = instance.get('tags', [])
        cursor = self.get_cursor(instance)
        self._fetch_metrics(cursor, instance)

    def get_sql_type(self, instance, counter_name):
        '''
        Return the type of the performance counter so that we can report it to
        Datadog correctly
        '''
        cursor = self.get_cursor(instance)
        cursor.execute("""
            select cntr_type
            from sys.dm_os_performance_counters
            where counter_name = ?
            """, (counter_name))
        (value,) = cursor.fetchone()
        if value == PERF_RAW_LARGE_BASE:
            self.log.warning("Metric %s is of type Base and shouldn't be reported this way")
        return value

    def _fetch_metrics(self, cursor, instance):
        ''' Fetch the metrics from the sys.dm_os_performance_counters table
        '''
        custom_tags = instance.get('tags', [])
        for metric in self.METRICS:
            # Normalize all rows to the same size for easy of use
            if len(metric) == 3:
                metric = metric + ('', None)
            elif len(metric) == 4:
                metric = metric + (None,)

            mname, mtype, counter, instance_n, tag_by = metric

            # For "ALL" instances, we run a separate method because we have
            # to loop over multiple results and tag the metrics
            if instance_n == ALL_INSTANCES:
                try:
                    self._fetch_all_instances(metric, cursor, custom_tags)
                except Exception, e:
                    self.log.exception('Unable to fetch metric: %s' % mname)
                    self.warning('Unable to fetch metric: %s' % mname)
            else:
                try:
                    cursor.execute("""
                        select cntr_value
                        from sys.dm_os_performance_counters
                        where counter_name = ?
                        and instance_name = ?
                    """, (counter, instance_n))
                    (value,) = cursor.fetchone()
                except Exception, e:
                    self.log.exception('Unable to fetch metric: %s' % mname)
                    self.warning('Unable to fetch metric: %s' % mname)
                    continue

                # Save the metric
                metric_func = getattr(self, mtype)
                metric_func(mname, value, tags=custom_tags)

    def _fetch_all_instances(self, metric, cursor, custom_tags):
        mname, mtype, counter, instance_n, tag_by = metric
        cursor.execute("""
            select instance_name, cntr_value
            from sys.dm_os_performance_counters
            where counter_name = ?
            and instance_name != '_Total'
        """, (counter,))
        rows = cursor.fetchall()

        for instance_name, cntr_value in rows:
            value = cntr_value
            tags = ['%s:%s' % (tag_by, instance_name.strip())] + custom_tags
            metric_func = getattr(self, mtype)
            metric_func(mname, value, tags=tags)

