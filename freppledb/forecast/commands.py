#
# Copyright (C) 2023 by frePPLe bv
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

import os
from datetime import datetime, timedelta
from psycopg2.extras import execute_batch
import tempfile
import logging
import sys
from time import time

from django.db import connections, transaction, DEFAULT_DB_ALIAS
from django.utils.translation import gettext_lazy as _

from .models import Forecast
from freppledb.boot import getAttributes
from freppledb.common.commands import PlanTaskRegistry, PlanTask, clean_value
from freppledb.common.models import Parameter, BucketDetail
from freppledb.common.report import getCurrentDate
from freppledb.input.commands.load import LoadTask
from freppledb.input.models import Item, Customer, Location


logger = logging.getLogger(__name__)


@PlanTaskRegistry.register
class CheckLicense(PlanTask):
    description = "Checking the license"
    sequence = 1

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        try:
            from frepple import demand_forecast

            return -1
        except ImportError:
            return 1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        logger.error("Forecasting module is not licensed.")
        logger.error(
            "Uncomment the forecasting app from your djangosettings.py file to run without this module."
        )
        sys.exit(1)


@PlanTaskRegistry.register
class PopulateForecastTable(PlanTask):
    description = "Populate forecast table and create root nodes"
    sequence = 80

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "fcst" in os.environ:
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):

        # Returns the parent for all customers, typically AllCustomers
        def getParentCustomer(cursor):
            cursor.execute("select name from customer c1 where lvl = 0")
            root_nb = cursor.rowcount
            if root_nb == 1:
                return cursor.fetchone()[0]
            else:
                return None

        # Assure the hierarchies are up to date and have only single root
        Item.createRootObject(database=database)
        Location.createRootObject(database=database)
        Customer.createRootObject(database=database)

        # Check value of parameter to check if this population should be done
        # missing parameter means false
        param = Parameter.getValue("forecast.populateForecastTable", database, "true")
        if param.lower() == "true":

            cursor = connections[database].cursor()
            parentCustomer = getParentCustomer(cursor)

            if parentCustomer:

                # We are in the case where there is a single root customer, typically "All customers"
                # We will add forecast records for all item/location/customer combination found in the demand
                # with a planned = false and a method = automatic
                # We will also add forecast records for "All customers" with planned = true and method = aggregate

                # Removing any forecast record for non existing (item,location) combinations in the demand
                deleted_rows = 0
                cursor.execute(
                    """
                    delete from forecast f
                    using (select distinct item_id, location_id from forecast
                    except select distinct item_id, location_id from demand
                    except select distinct item_id, location_id from forecastplan where value ?| array['ordersadjustment','forecastoverride']) t
                    where t.item_id = f.item_id
                    and t.location_id = f.location_id
                    """
                )
                deleted_rows = cursor.rowcount

                # Removing any forecast record for leaf combinations where (item,location,customer) in the demand
                cursor.execute(
                    """
                    with cte as (select item_id, location_id from forecastplan
                      where value ?| array['ordersadjustment','forecastoverride'])
                    delete from forecast
                    using (select item_id, location_id, customer_id from forecast
                          except select distinct item_id, location_id, customer_id from demand) d, cte
                    where forecast.customer_id != %s
                    and forecast.item_id = d.item_id
                    and forecast.location_id = d.location_id
                    and forecast.customer_id = d.customer_id
                    and (forecast.item_id, forecast.location_id) not in (select * from cte)
                    """,
                    (parentCustomer,),
                )

                logger.info(
                    "Removing %s records from forecast table"
                    % (deleted_rows + cursor.rowcount)
                )

                # Adding the missing records into forecast table for the root customer (All customers).
                cursor.execute(
                    """
                    with cte as
                    (select distinct item_id, location_id, %s as customer_id from demand
                    except select item_id, location_id, customer_id from forecast
                    where customer_id = %s)
                    insert into forecast
                    (name, item_id, location_id, customer_id, method, priority, discrete, planned, lastmodified)
                    select distinct
                      item_id||' @ '||location_id||' @ '||customer_id, item_id, location_id, customer_id, 'automatic',
                      20, true, true, now()
                    from cte
                    """,
                    (parentCustomer, parentCustomer),
                )
                added_rows = cursor.rowcount
                # Adding the missing records into forecast table at the customer level.
                cursor.execute(
                    """
                    with cte as(
                      select distinct item_id, location_id, customer_id from demand
                      except select item_id, location_id, customer_id from forecast)
                      insert into forecast
                      (name, item_id, location_id, customer_id, method, priority, discrete, planned, lastmodified)
                    select distinct
                      item_id||' @ '||location_id||' @ '||customer_id, item_id, location_id,
                      customer_id, 'automatic', 20, true, false, now()
                    from cte
                    """
                )
                logger.info(
                    "Adding %s records into forecast table"
                    % (added_rows + cursor.rowcount)
                )
        else:
            logger.info(
                "Parameter forecast.populateForecastTable set to false: skipping this step."
            )


@PlanTaskRegistry.register
class CalculateDemandPattern(PlanTask):
    description = "Calculate demand pattern"
    sequence = 83

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "fcst" in os.environ:
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        starttime = time()
        cursor = connections[database].cursor()
        fcst_calendar = Parameter.getValue("forecast.calendar", database, None)
        horizon_history = int(
            Parameter.getValue("forecast.Horizon_history", database, 10000)
        )
        currentdate = getCurrentDate(database)

        # Create a temporary table for the buckets
        cursor.execute(
            """
            create temporary table bucket_tmp on commit preserve rows as
            select row_number() over () as id, startdate, enddate from common_bucketdetail
            where bucket_id = %s
            and startdate >= %s - ('%s days')::interval and enddate <= %s
            order by startdate
            """,
            (fcst_calendar, currentdate, horizon_history, currentdate),
        )
        cursor.execute(
            """
            create unique index bucket_tmp_idx1 on bucket_tmp (startdate)
            """
        )

        # find id for current period -1
        cursor.execute("select max(id) from bucket_tmp")
        lastPeriod = cursor.fetchone()[0]

        # Analyze demand
        cursor.execute(
            """
            create temporary table item_pattern on commit preserve rows as
            select
              item_id, coalesce(pow(stddev(quantity)/avg(quantity),2),0) CV2,
              count(*) as demand_buckets, min(id) as earliest_bucket
            from
            (
            select item_id, id, sum(quantity) quantity
            from demand
            inner join bucket_tmp
              on demand.due between bucket_tmp.startdate and bucket_tmp.enddate
            group by item_id, id
            having sum(quantity) > 0
            ) demand
            where exists (select 1 from forecast where forecast.item_id = demand.item_id)
            group by item_id
            """
        )
        cursor.execute(
            "create unique index item_pattern_idx_1 on item_pattern (item_id)"
        )

        # Merge results
        cursor.execute("update item set adi = null, cv2 = null, demand_pattern = null")
        cursor.execute(
            """
            update item
            set demand_pattern = case when t.adi < 1.32 and t.cv2 < 0.49 then 'smooth'
                                   when t.adi < 1.32 and t.cv2 >= 0.49 then 'erratic'
                                   when t.adi >= 1.32 and t.cv2 < 0.49 then 'intermittent'
                                   else 'lumpy' end,
                adi = t.adi,
                cv2 = t.cv2
            from (
              select item_id, cv2,
                (%s-earliest_bucket+1)/demand_buckets::numeric(20,8) adi
              from item_pattern
            ) t
            where item.name = t.item_id
            """,
            (lastPeriod,),
        )

        # Drop temporary tables
        cursor.execute("drop table item_pattern")
        cursor.execute("drop table bucket_tmp")
        logger.info("Calculated demand pattern in %.2f seconds" % (time() - starttime))


@PlanTaskRegistry.register
class AggregateDemand(PlanTask):
    description = "Aggregate demand"
    sequence = 82

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "fcst" in os.environ or "supply" in os.environ:
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if (
            Parameter.getValue(
                "forecast.new_demand_aggregation", database, "false"
            ).lower()
            == "true"
        ):
            cls.run_new(database=database, **kwargs)
        else:
            cls.run_old(database=database, **kwargs)

    @classmethod
    def run_new(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        with connections[database].cursor() as cursor:

            fcst_calendar = Parameter.getValue("forecast.calendar", database, None)
            horizon_future = int(
                Parameter.getValue("forecast.Horizon_future", database, 365)
            )
            horizon_history = int(
                Parameter.getValue("forecast.Horizon_history", database, 10000)
            )
            currentdate = getCurrentDate(database)

            # Delete forecastplan records for invalid dates
            starttime = time()

            # Creating temp tables
            cursor.execute(
                """
                create temporary table item_hierarchy on commit preserve rows as
                select parent.name parent, child.name child from item parent
                inner join item child
                on child.lft between parent.lft and parent.rght
                """
            )
            cursor.execute(
                """
                create index item_hierarchy_idx on item_hierarchy (child)
                """
            )
            cursor.execute(
                """
                create temporary table location_hierarchy on commit preserve rows as
                select parent.name parent, child.name child from location parent
                inner join location child
                on child.lft between parent.lft and parent.rght
                """
            )
            cursor.execute(
                "create index location_hierarchy_idx on location_hierarchy (child)"
            )
            cursor.execute(
                """
                create temporary table customer_hierarchy on commit preserve rows as
                select parent.name parent, child.name child from customer parent
                inner join customer child
                on child.lft between parent.lft and parent.rght
                """
            )
            cursor.execute(
                "create index customer_hierarchy_idx on customer_hierarchy (child)"
            )
            cursor.execute(
                """
                drop table if exists forecasthierarchy;
                create table forecasthierarchy as
                select distinct
                item_hierarchy.parent item_id, location_hierarchy.parent location_id,
                customer_hierarchy.parent customer_id
                from forecast
                inner join item_hierarchy on forecast.item_id = item_hierarchy.child
                inner join customer_hierarchy on forecast.customer_id = customer_hierarchy.child
                inner join location_hierarchy on forecast.location_id = location_hierarchy.child
                where coalesce(method, 'automatic') != 'aggregate'
                """
            )
            cursor.execute(
                "create unique index nodes on forecasthierarchy (item_id, location_id, customer_id)"
            )
            cursor.execute(
                "drop table item_hierarchy, location_hierarchy, customer_hierarchy"
            )

            cursor.execute(
                """
                delete from forecastplan
                where (startdate, enddate) not in  (
                select startdate, enddate
                from common_bucketdetail
                where bucket_id = %s
                and startdate >= %s
                and startdate < %s
                and enddate > least((select coalesce(min(due),'2000-01-01 00:00:00'::timestamp) from demand),
                                    %s)
                )
                """,
                (
                    fcst_calendar,
                    currentdate - timedelta(days=horizon_history),
                    currentdate + timedelta(days=horizon_future),
                    currentdate,
                ),
            )
            transaction.commit(using=database)
            logger.info(
                "Aggregate - deleted %d obsolete forecast buckets in %.2f seconds"
                % (cursor.rowcount, time() - starttime)
            )

            # Main aggregation
            starttime = time()
            cursor.execute(
                """
                begin;
                call aggregatedemand(%s, %s, %s);
                end;
                """,
                (
                    fcst_calendar,
                    currentdate - timedelta(days=horizon_history),
                    currentdate + timedelta(days=horizon_future),
                ),
            )
            logger.info(
                "Aggregate - aggregated demand information in %.2f seconds"
                % (time() - starttime)
            )

            # Pruning empty records
            starttime = time()
            cursor.execute(
                """
                delete from forecastplan
                where value = '{}'::jsonb or value is null
                """
            )
            transaction.commit(using=database)
            logger.info(
                "Aggregate - pruned %d empty records in %.2f seconds"
                % (cursor.rowcount, time() - starttime)
            )

            # Pruning dangling records, ie records that have no child any longer
            # in the forecast table
            starttime = time()
            cursor.execute(
                """
                with cte as (
                   select distinct item_id, location_id, customer_id
                   from forecastplan
                   )
                delete from forecastplan
                where (item_id, location_id, customer_id) in (
                    select
                      item_id, location_id, customer_id
                    from cte
                    where not exists (
                      select 1
                      from forecast
                      inner join item
                        on forecast.item_id = item.name
                      inner join location
                        on forecast.location_id = location.name
                      inner join customer
                        on forecast.customer_id = customer.name
                      inner join item as fitem on
                        cte.item_id = fitem.name
                      inner join location as flocation
                        on cte.location_id = flocation.name
                      inner join customer as fcustomer
                        on cte.customer_id = fcustomer.name
                      where item.lft between fitem.lft and fitem.rght
                        and location.lft between flocation.lft and flocation.rght
                        and customer.lft between fcustomer.lft and fcustomer.rght
                    )
                )
                """
            )
            transaction.commit(using=database)
            logger.info(
                "Aggregate - pruned %d dangling records in %.2f seconds"
                % (cursor.rowcount, time() - starttime)
            )

    @classmethod
    def run_old(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        cursor = connections[database].cursor()

        fcst_calendar = Parameter.getValue("forecast.calendar", database, None)
        horizon_future = int(
            Parameter.getValue("forecast.Horizon_future", database, 365)
        )
        horizon_history = int(
            Parameter.getValue("forecast.Horizon_history", database, 10000)
        )
        currentdate = getCurrentDate(database)

        # Creating temp tables
        starttime = time()
        cursor.execute(
            """
            create temporary table item_hierarchy on commit preserve rows as
            select parent.name parent, child.name child from item parent
            inner join item child
            on child.lft between parent.lft and parent.rght
            """
        )
        cursor.execute(
            """
            create index item_hierarchy_idx on item_hierarchy (child)
            """
        )
        cursor.execute(
            """
            create temporary table location_hierarchy on commit preserve rows as
            select parent.name parent, child.name child from location parent
            inner join location child
            on child.lft between parent.lft and parent.rght
            """
        )
        cursor.execute(
            "create index location_hierarchy_idx on location_hierarchy (child)"
        )
        cursor.execute(
            """
            create temporary table customer_hierarchy on commit preserve rows as
            select parent.name parent, child.name child from customer parent
            inner join customer child
            on child.lft between parent.lft and parent.rght
            """
        )
        cursor.execute(
            "create index customer_hierarchy_idx on customer_hierarchy (child)"
        )
        cursor.execute(
            """
            drop table if exists forecasthierarchy;
            create table forecasthierarchy as
            select distinct
              item_hierarchy.parent item_id, location_hierarchy.parent location_id,
              customer_hierarchy.parent customer_id
            from forecast
            inner join item_hierarchy on forecast.item_id = item_hierarchy.child
            inner join customer_hierarchy on forecast.customer_id = customer_hierarchy.child
            inner join location_hierarchy on forecast.location_id = location_hierarchy.child
            where coalesce(method, 'automatic') != 'aggregate'
            """
        )
        cursor.execute(
            "create unique index nodes on forecasthierarchy (item_id, location_id, customer_id)"
        )
        logger.info(
            "Aggregate - creating temp tables in %.2f seconds" % (time() - starttime)
        )

        # Delete forecastplan records for invalid dates
        starttime = time()
        # TO DO : demand history could be only composed of order adjustments with an empty demand table
        cursor.execute(
            """
            delete from forecastplan
            where (startdate, enddate) not in  (
              select startdate, enddate
              from common_bucketdetail
              where bucket_id = %s
              and startdate >= %s
              and startdate < %s
              and enddate > least((select coalesce(min(due),'2000-01-01 00:00:00'::timestamp) from demand),
                                  %s)
              )
            """,
            (
                fcst_calendar,
                currentdate - timedelta(days=horizon_history),
                currentdate + timedelta(days=horizon_future),
                currentdate,
            ),
        )
        transaction.commit(using=database)
        logger.info(
            "Aggregate - deleted %d obsolete forecast buckets in %.2f seconds"
            % (cursor.rowcount, time() - starttime)
        )

        # Delete forecastplan for invalid (item, location, customer) combinations
        starttime = time()
        cursor.execute(
            """
            delete from forecastplan
            using (select distinct item_id, location_id, customer_id from forecastplan
            except select item_id, location_id, customer_id from forecasthierarchy) t
            where forecastplan.item_id = t.item_id
            and forecastplan.location_id = t.location_id
            and forecastplan.customer_id = t.customer_id
            """
        )
        transaction.commit(using=database)
        logger.info(
            "Aggregate - deleted %d invalid combinations in %.2f seconds"
            % (cursor.rowcount, time() - starttime)
        )

        # reset leaf nodes with no more open/total orders
        starttime = time()

        cursor.execute(
            """
            create temporary table demand_agg on commit preserve rows as
            select startdate, enddate,
            item_hierarchy.parent item_id, location_hierarchy.parent location_id,
            customer_hierarchy.parent customer_id,
            greatest(sum(case when coalesce(status, 'open') in ('open','quote') then quantity else 0 end), 0) ordersopen,
            greatest(sum(quantity),0) orderstotal,
            greatest(sum(case when coalesce(status, 'open') in ('open','quote') then quantity*item.cost else 0 end), 0) ordersopenvalue,
            greatest(sum(quantity*item.cost), 0)  orderstotalvalue
            from demand
            inner join item on item.name = demand.item_id
            inner join item_hierarchy on demand.item_id = item_hierarchy.child
            inner join customer_hierarchy on demand.customer_id = customer_hierarchy.child
            inner join location_hierarchy on demand.location_id = location_hierarchy.child
            inner join forecast on forecast.item_id = item_hierarchy.parent
            and forecast.customer_id = customer_hierarchy.parent
            and forecast.location_id = location_hierarchy.parent
            and coalesce(forecast.method, 'automatic') != 'aggregate'
            inner join common_bucketdetail cb
                  on cb.bucket_id = %s
                  and cb.startdate <= due
                  and due < cb.enddate
            where %s <= cb.startdate and cb.startdate < %s and coalesce(demand.status, 'open') != 'canceled'
            group by startdate, enddate, item_hierarchy.parent, location_hierarchy.parent,
            customer_hierarchy.parent
            having greatest(sum(quantity),0) != 0 or greatest(sum(case when coalesce(status, 'open') in ('open','quote') then quantity else 0 end), 0) != 0;
            create unique index on demand_agg (item_id, location_id, customer_id, startdate);
        """,
            (
                fcst_calendar,
                currentdate - timedelta(days=horizon_history),
                currentdate + timedelta(days=horizon_future),
            ),
        )

        #
        cursor.execute(
            """
            create temporary table leaf_nomore_orders on commit preserve rows as
            (select forecastplan.item_id, forecastplan.location_id, forecastplan.customer_id,
            forecastplan.startdate, 'ordersopen' measure from forecastplan
            inner join forecast on forecast.item_id = forecastplan.item_id
                                and forecast.location_id = forecastplan.location_id
                                and forecast.customer_id = forecastplan.customer_id
            where forecastplan.value ? 'ordersopen'
            and coalesce(forecast.method, 'automatic') != 'aggregate'
            except
            select item_id, location_id, customer_id, startdate, 'ordersopen' from demand_agg
            where ordersopen > 0)
            union all
            (select forecastplan.item_id, forecastplan.location_id, forecastplan.customer_id,
            forecastplan.startdate, 'orderstotal' measure from forecastplan
            inner join forecast on forecast.item_id = forecastplan.item_id
                                and forecast.location_id = forecastplan.location_id
                                and forecast.customer_id = forecastplan.customer_id
            where forecastplan.value ? 'orderstotal'
            and coalesce(forecast.method, 'automatic') != 'aggregate'
            except
            select item_id, location_id, customer_id, startdate, 'orderstotal' from demand_agg
            where orderstotal > 0);

            create index on leaf_nomore_orders (item_id, location_id, customer_id, startdate);
            """
        )

        cursor.execute(
            """
           update forecastplan set value = (value - leaf_nomore_orders.measure) - (leaf_nomore_orders.measure||'value')
           from leaf_nomore_orders
           where forecastplan.item_id = leaf_nomore_orders.item_id
           and forecastplan.location_id = leaf_nomore_orders.location_id
           and forecastplan.customer_id = leaf_nomore_orders.customer_id
           and forecastplan.startdate = leaf_nomore_orders.startdate
           """
        )

        logger.info(
            "Aggregate - reset %d leaf node records with no more open/total in %.2f seconds"
            % (cursor.rowcount, time() - starttime)
        )

        # updating open/total orders values
        starttime = time()
        cursor.execute(
            """
            insert into forecastplan (item_id, location_id, customer_id, startdate, enddate, value)
            select item_id, location_id, customer_id, startdate, enddate, jsonb_strip_nulls(
                                          jsonb_build_object('orderstotal', demand_agg.orderstotal,
                                          'orderstotalvalue', demand_agg.orderstotalvalue,
                                          'ordersopen', case when demand_agg.ordersopen = 0 then null else ordersopen end,
                                          'ordersopenvalue', case when demand_agg.ordersopenvalue = 0 then null else ordersopenvalue end)
                                          ) as value
            from demand_agg
            on conflict (item_id, location_id, customer_id, startdate)
            do update set value =  forecastplan.value || excluded.value
            where
              (excluded.value->>'orderstotal')::numeric is distinct from (forecastplan.value->>'orderstotal')::numeric
              or (excluded.value->>'ordersopen')::numeric is distinct from (forecastplan.value->>'ordersopen')::numeric
              or (excluded.value->>'orderstotalvalue')::numeric is distinct from (forecastplan.value->>'orderstotalvalue')::numeric
              or (excluded.value->>'ordersopenvalue')::numeric is distinct from (forecastplan.value->>'ordersopenvalue')::numeric
            """
        )
        logger.info(
            "Aggregate - updating orders open/total in %.2f seconds"
            % (time() - starttime)
        )

        # updating open/total orders values
        starttime = time()
        cursor.execute(
            """
            delete from forecastplan
            where (value = '{}' or value is null)
            """
        )
        transaction.commit(using=database)
        logger.info(
            "Aggregate - pruned %d empty records in %.2f seconds"
            % (cursor.rowcount, time() - starttime)
        )

        # Wrapping up
        starttime = time()
        cursor.execute("drop table item_hierarchy")
        cursor.execute("drop table location_hierarchy")
        cursor.execute("drop table customer_hierarchy")
        cursor.execute("drop table demand_agg")
        cursor.execute("drop table leaf_nomore_orders")
        logger.info("Aggregate - wrapping up in %.2f seconds" % (time() - starttime))


@PlanTaskRegistry.register
class LoadMeasures(PlanTask):
    description = "Load measures"
    sequence = 90.25

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        return 0.1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        cnt = 0
        starttime = time()
        with connections[database].cursor() as cursor:
            cursor.execute(
                """
                insert into measure
                  (name, label, type, mode_future, mode_past, description,
                   compute_expression, formatter, initially_hidden, defaultvalue,
                   lastmodified)
                values (
                  'forecasttotal', 'total forecast', 'computed', 'view', 'view',
                  'This row is what we''ll plan supply for',
                  'if(forecastoverride == -1, forecastbaseline, forecastoverride)',
                  'number', false, 0, now()
                  )
                on conflict (name) do nothing
                """
            )
            cursor.execute(
                """
                select
                  type, name, discrete, compute_expression, update_expression, defaultvalue, overrides
                from measure
                order by overrides nulls first, name
                """
            )
            for i in cursor:
                cnt += 1
                try:
                    m = None
                    if i[0] == "computed":
                        m = frepple.measure_computed(
                            name=i[1],
                            discrete=i[2],
                            compute_expression=i[3],
                            update_expression=i[4],
                            default=i[5],
                        )
                    elif i[0] == "local":
                        m = frepple.measure_local(
                            name=i[1], discrete=i[2], default=i[5]
                        )
                    elif i[0] == "aggregate":
                        m = frepple.measure_aggregated(
                            name=i[1], discrete=i[2], default=i[5]
                        )
                    if m and i[6]:
                        m.overrides = frepple.measure(name=i[6], action="C")
                except Exception as e:
                    logger.error("**** %s ****" % e)
        logger.info("Loaded %d measures in %.2f seconds" % (cnt, time() - starttime))
        frepple.compileMeasures()
        logger.info("Successfully compiled expressions")


@PlanTaskRegistry.register
class LoadForecast(LoadTask):
    description = "Load forecast"
    sequence = 107.5

    calendar = None

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        cls.calendar = Parameter.getValue("forecast.calendar", database, None)
        if not cls.calendar:
            logger.warning(
                "Warning: parameter forecast.calendar not set. No forecast will be calculated."
            )
            return -1
        else:
            return 1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        if cls.filter:
            filter_where = " and %s " % cls.filter
        else:
            filter_where = ""

        attrs = [f[0] for f in getAttributes(Forecast)]
        if attrs:
            attrsql = ", %s" % ", ".join(attrs)
        else:
            attrsql = ""

        createForecastSolver(database, cls.task)

        horizon_history = int(
            Parameter.getValue("forecast.Horizon_history", database, 10000)
        )
        horizon_future = int(
            Parameter.getValue("forecast.Horizon_future", database, 365)
        )
        with connections[database].cursor() as cursor:
            cursor.execute(
                "select extract(day from %s - min(startdate)) from forecastplan",
                (frepple.settings.current.date(),),
            )
            oldest = cursor.fetchone()[0]
            if oldest and oldest < horizon_history:
                horizon_history = oldest

        cnt = 0
        starttime = time()
        with transaction.atomic(using=database):
            with connections[database].chunked_cursor() as cursor:
                cursor.execute(
                    """
                    select
                    name, customer_id, item_id, location_id, priority,
                    minshipment, discrete, maxlateness,
                    category, subcategory, coalesce(method,'automatic'), planned, operation_id %s
                    from forecast
                    where coalesce(method,'automatic') <> 'aggregate'
                    and item_id is not null
                    and location_id is not null
                    and customer_id is not null %s
                    """
                    % (
                        attrsql,
                        filter_where,
                    )
                )
                for i in cursor:
                    try:
                        cnt += 1
                        fcst = frepple.demand_forecast(
                            name=i[0],
                            customer=frepple.customer(name=i[1]),
                            item=frepple.item(name=i[2]),
                            location=frepple.location(name=i[3]),
                            priority=i[4],
                            category=i[8],
                            subcategory=i[9],
                            horizon_history=horizon_history,
                            horizon_future=horizon_future,
                        )
                        if i[12]:
                            fcst.operation = frepple.operation(name=i[12])
                        if i[5] is not None:
                            fcst.minshipment = i[5]
                        if not i[6]:
                            fcst.discrete = False  # null value -> False
                        if i[7] is not None:
                            fcst.maxlateness = i[7].total_seconds()
                        if i[10]:
                            fcst.methods = i[10]
                        fcst.planned = i[11]
                        idx = 13
                        for a in attrs:
                            setattr(fcst, a, i[idx])
                            idx += 1
                    except Exception as e:
                        logger.error("**** %s ****" % e)
        logger.info("Loaded %d forecasts in %.2f seconds" % (cnt, time() - starttime))


@PlanTaskRegistry.register
class ExportStaticForecast(PlanTask):

    description = ("Export static data", "Export forecast")
    sequence = (305, "exportstatic2", 5)

    @classmethod
    def getWeight(cls, exportstatic=False, **kwargs):
        return 1 if exportstatic else -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        source = kwargs.get("source", None)
        attrs = [f[0] for f in getAttributes(Forecast)]

        def getData():
            for i in frepple.demands():
                if (
                    not isinstance(i, frepple.demand_forecast)
                    or i.hidden
                    or (source and source != i.source)
                ):
                    continue
                r = [
                    i.name,
                    i.customer and i.customer.name or None,
                    i.item.name,
                    i.location and i.location.name or None,
                    i.priority,
                    round(i.minshipment, 8),
                    i.discrete,
                    i.planned,
                    i.maxlateness,
                    i.category,
                    i.subcategory,
                    i.operation.name
                    if i.operation and not i.operation.hidden
                    else None,
                    i.methods,
                    i.method,
                    i.source,
                    cls.timestamp,
                ]
                for a in attrs:
                    r.append(getattr(i, a, None))
                yield r

        with connections[database].cursor() as cursor:
            if source:
                cursor.execute(
                    """
                    delete from forecast
                    where source = %s
                    """,
                    (source,),
                )
            execute_batch(
                cursor,
                """
                insert into forecast
                (name,customer_id,item_id,location_id,priority,minshipment,
                 discrete,planned,maxlateness,category,subcategory,operation_id, method, out_method, source,lastmodified%s)
                values(%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s * interval '1 second',%%s,%%s,%%s,%%s,%%s,%%s,%%s%s)
                on conflict (name)
                do update set
                  customer_id=excluded.customer_id,
                  item_id=excluded.item_id,
                  location_id=excluded.location_id,
                  priority=excluded.priority,
                  minshipment=excluded.minshipment,
                  discrete=excluded.discrete,
                  planned=excluded.planned,
                  maxlateness=excluded.maxlateness,
                  category=excluded.category,
                  subcategory=excluded.subcategory,
                  operation_id=excluded.operation_id,
                  method=excluded.method,
                  out_method=excluded.out_method,
                  source=excluded.source,
                  lastmodified=excluded.lastmodified
                  %s
                """
                % (
                    "".join([",%s" % i for i in attrs]),
                    ",%s" * len(attrs),
                    "".join([",\n%s=excluded.%s" % (i, i) for i in attrs]),
                ),
                getData(),
            )


def createForecastSolver(db, task=None):
    import frepple

    # Initialize the solver
    horizon_future = None
    calendar = None
    try:
        kw = {}
        for param in (
            Parameter.objects.all()
            .using(db)
            .filter(name__startswith="forecast.")
            .exclude(name="forecast.populateForecastTable")
        ):
            key = param.name[9:]
            if key == "Horizon_future":
                try:
                    horizon_future = int(param.value)
                except Exception:
                    logger.error('Incorrect parameter "forecast.Horizon_future"')
                    return None
            elif key == "Horizon_history":
                try:
                    int(param.value)
                except Exception:
                    logger.error('Incorrect parameter "forecast.Horizon_history"')
                    return None
            elif key in ("DueWithinBucket",):
                try:
                    kw[key] = param.value
                except Exception:
                    logger.error('Incorrect parameter "forecast.%s"' % key)
            elif key == "calendar":
                try:
                    kw[key] = frepple.calendar(name=param.value, action="C")
                except Exception:
                    logger.warning("Parameter forecast.calendar not configured.")
                    return None
                calendar = param.value
            elif key == "Net_PastDemand":
                kw[key] = (param.value.lower() == "true") if param.value else False
            elif key == "AverageNoDataDays":
                kw[key] = (param.value.lower() == "true") if param.value else True
            elif key == "Net_IgnoreLocation":
                kw[key] = (param.value.lower() == "true") if param.value else False
            elif key in (
                "Iterations",
                "loglevel",
                "Skip",
                "MovingAverage_order",
                "Net_CustomerThenItemHierarchy",
                "Net_MatchUsingDeliveryOperation",
                "DeadAfterInactivity",
            ):
                try:
                    kw[key] = int(param.value)
                except Exception:
                    logger.error('Incorrect parameter "forecast.%s"' % key)
            elif key in ("Net_NetEarly", "Net_NetLate"):
                try:
                    kw[key] = int(param.value) * 86400
                except Exception:
                    logger.error('Incorrect parameter "forecast.%s"' % key)
            else:
                try:
                    kw[key] = float(param.value)
                except Exception:
                    logger.error('Incorrect parameter "forecast.%s"' % key)

        # Check whether we have forecast buckets to cover the complete forecasting horizon
        if horizon_future and calendar:
            currentdate = getCurrentDate(db)
            if (
                not BucketDetail.objects.all()
                .using(db)
                .filter(
                    enddate__gt=currentdate + timedelta(days=horizon_future),
                    bucket_id=calendar,
                )
                .exists()
            ):
                logger.warning(
                    "Bucket dates table doesn't cover the complete forecasting horizon"
                )

        return frepple.solver_forecast(**kw)
    except Exception as e:
        logger.warning("No forecasting solver can be created: %s", e)
        return None


@PlanTaskRegistry.register
class ValidateAggregatedData(PlanTask):
    description = "Validate aggregated forecast data"
    sequence = 169
    label = (
        "fcst",
        _("Generate forecast"),
        _(
            "Analyze the sales history and compute a statistical forecast for the future"
        ),
    )

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        return -1 if kwargs.get("skipLoad", False) else 1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        # TODO trigger recalculation at leafs?

        # Make sure all aggregated measures sum up correctly at higher levels
        # We can skip validating the measures of type "planned" when we will be replanning shortly
        frepple.aggregateMeasures(includeplanned="supply" not in os.environ)

        # Reduce the forecast cache to max 500 objects to save memory.
        # For a web service start, we do this right after this validation.
        # When a plan is generated, we do this when the plan is generated and forecast is exported.
        if "loadplan" in os.environ:
            frepple.cache.flush()
            frepple.cache.write_immediately = True
            if frepple.cache.maximum > 500:
                frepple.cache.maximum = 500


@PlanTaskRegistry.register
class CalculateForecast(PlanTask):
    description = "Calculate statistical forecast and forecast consumption"
    sequence = 170
    label = ("fcst", _("Generate forecast"))

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "fcst" in os.environ or "supply" in os.environ:
            if not Parameter.getValue("forecast.calendar", database, None):
                return -1
            else:
                return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        # The argument specifies whether we run "forecasting + netting" or "netting"
        slvr = createForecastSolver(database, cls.task)
        if not slvr:
            raise Exception("Can't compute a statistical forecast")
        slvr.solve("fcst" in os.environ)


@PlanTaskRegistry.register
class ExportForecastMetrics(PlanTask):
    description = "Export forecast metrics"
    sequence = 171
    label = ("fcst", _("Generate forecast"))

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "fcst" in os.environ:
            if not Parameter.getValue("forecast.calendar", database, None):
                return -1
            else:
                return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        cursor = connections[database].cursor()
        cursor.execute(
            "update forecast set out_smape=null, out_method=null, out_deviation=null"
        )
        cursor.execute(
            """
            create temporary table forecast_tmp
            (
            name character varying(300),
            out_smape numeric(20,8),
            out_method character varying(20),
            out_deviation numeric(20,8)
            )
            on commit preserve rows
            """
        )

        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as tmp:
            for i in frepple.demands():
                if isinstance(i, frepple.demand_forecast):
                    print(
                        "%s\v%s\v%s\v%s"
                        % (
                            clean_value(i.name),
                            i.smape_error * 100,
                            i.method,
                            i.deviation,
                        ),
                        file=tmp,
                    )
            tmp.seek(0)
            cursor.copy_from(file=tmp, table="forecast_tmp", sep="\v")
            tmp.close()

        cursor.execute("create unique index forecast_tmp_idx on forecast_tmp (name)")
        cursor.execute(
            """
            update forecast
              set out_smape = tmp.out_smape,
                  out_method = tmp.out_method,
                  out_deviation = tmp.out_deviation
            from forecast_tmp tmp
            where tmp.name = forecast.name
            """
        )
        cursor.execute("drop table forecast_tmp")
        cursor.execute("vacuum analyze forecast")


def exportSingleForecastMetrics(fcst_name, db=DEFAULT_DB_ALIAS):
    """
    Export metrics and forecast methods for a single forecast.

    This method is called from the incremental replanning in the inventory
    planning screen.
    """
    import frepple
    from freppledb.forecast.models import Forecast

    fcst = Forecast.objects.all().using(db).get(pk=fcst_name)
    fcst_frepple = frepple.demand(name=fcst_name)
    fcst.method = fcst_frepple.methods
    fcst.out_smape = fcst_frepple.smape_error * 100
    fcst.out_method = fcst_frepple.method
    fcst.out_deviation = fcst_frepple.deviation
    fcst.save(using=db)


@PlanTaskRegistry.register
class ExportForecast(PlanTask):
    description = "Export forecast data"
    sequence = (401, "export3", 1)
    label = ("fcst", _("Generate forecast"))
    export = True

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if ("fcst" in os.environ or "supply" in os.environ) and Parameter.getValue(
            "forecast.calendar", database, None
        ):
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import frepple

        if "supply" in os.environ:
            frepple.updatePlannedForecast()
        frepple.cache.flush()
        frepple.cache.write_immediately = True
        if frepple.cache.maximum > 500:
            # Reduce the forecast cache to max 500 objects to save memory
            frepple.cache.maximum = 500

        # refresh materialized view
        with connections[database].cursor() as cursor:
            cursor.execute("REFRESH MATERIALIZED VIEW forecastreport_view")
