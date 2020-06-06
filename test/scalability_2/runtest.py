#!/usr/bin/env python3
#
# Copyright (C) 2007 by frePPLe bv
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

# This script is a simple, generic model generator. A number of different
# models are created with varying number of clusters, depth of the supply path
# and number of demands per cluster. By evaluating the runtime of these models
# we can evaluate different aspects of Frepple's scalability.
#
# This test script is meant more as a sample for your own tests on evaluating
# scalability.
#
# The autogenerated supply network looks schematically as follows:
#   [ Operation -> buffer ] ...   [ -> Operation -> buffer ]  [ Delivery ]
#   [ Operation -> buffer ] ...   [ -> Operation -> buffer ]  [ Delivery ]
#   [ Operation -> buffer ] ...   [ -> Operation -> buffer ]  [ Delivery ]
#   [ Operation -> buffer ] ...   [ -> Operation -> buffer ]  [ Delivery ]
#   [ Operation -> buffer ] ...   [ -> Operation -> buffer ]  [ Delivery ]
#       ...                                  ...
# Each row represents a cluster.
# The operation+buffer are repeated as many times as the depth of the supply
# path parameter.
# In each cluster a single item is defined, and a parametrizable number of
# demands is placed on the cluster.

import os
import sys
import random
from time import time


# This function generates a random date
def getDate():
  month = "%02d" % (int(random.uniform(0, 12)) + 1)
  day = "%02d" % (int(random.uniform(0, 28)) + 1)
  return "2007-%s-%sT00:00:00" % (month, day)


# This routine creates the model data file.
# The return value is an indication of the size of the model.
def create(cluster, demand, level):
  # Initialize
  size = 0
  out = open("input.xml", "wt")
  print("<plan xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">", file=out)

  # Items
  print("<items>", file=out)
  for i in range(cluster):
    ++size
    print("<item name=\"Item C%d\"/>" % i, file=out)
  print("</items>", file=out)

  # Demands
  print("<demands>", file=out)
  for i in range(cluster):
    for j in range(demand):
      size += 2  # since a demand will result in multiple operationplans
      print((
        "<demand name=\"Demand C%dD%d\" " +
        "quantity=\"1\" due=\"%s\">" +
        "<location name=\"factory\"/>" +
        "<operation name=\"factory\"/>" +
        "<item name=\"Item C%d\"/></demand>"
        ) % (i, j, getDate(), i), file=out)
  print("</demands>", file=out)

  # Operations
  print("<operations>", file=out)
  for i in range(cluster):
    print((
      "<operation name=\"Del C%d\"> <flows>" +
      "<flow xsi:type=\"flow_start\" quantity=\"-1\">" +
      "<buffer name=\"Buffer C%dL1\"/></flow>" +
      "</flows></operation>") % (i, i),
      file=out
      )
    for j in range(level):
      size += 2
      print((
        "<operation name=\"Oper C%dO%d\" " +
        "xsi:type=\"operation_fixed_time\" " +
        "duration=\"PT%dH\"> <flows>" +
        "<flow xsi:type=\"flow_end\" quantity=\"1\">" +
        "<buffer name=\"Buffer C%dL%d\">" +
        "<producing name=\"Oper C%dO%d\"/></buffer></flow>" +
        "<flow xsi:type=\"flow_start\" quantity=\"-1\">" +
        "<buffer name=\"Buffer C%dL%d\"/></flow>" +
        "</flows><location name=\"factory\"/></operation>"
        ) % (i, j, 24 * int(random.uniform(0, 10) + 1), i, j, i, j, i, j + 1),
        file=out
        )

  # Create material supply
  for i in range(cluster):
    print((
      "<operation name=\"Supply C%d\"> " +
      "<location name=\"factory\"/>" +
      "<flows><flow xsi:type=\"flow_end\" quantity=\"1\">" +
      "<buffer name=\"Buffer C%dL%d\"/>" +
      "</flow></flows></operation>") % (i, i, level + 1),
      file=out
      )
  print("</operations>\n<operationplans>", file=out)
  for i in range(cluster):
    print((
      "<operationplan id=\"%d\" " +
      "start=\"2007-05-01T00:00:00\" quantity=\"%d\" " +
      "status=\"confirmed\"><operation name=\"Supply C%d\"/></operationplan>"
      ) % (i + 2, demand, i),
      file=out
      )
  print("</operationplans>", file=out)

  # Tail of the output file
  print("</plan>", file=out)
  out.close()

  # Return size indication
  return size


# Initialize random number generator in a reproducible way
random.seed(100)

# Loop over all cluster values
runtimes = {}
print("Clusters\tDemands\tLevels\tRuntime")
for cluster in [100, 200, 300]:

  # Loop over all demand values
  for demand in [10, 20, 30]:

    # Loop over all level values
    for level in [1, 5, 9]:

      # Creating model data file
      size = create(cluster, demand, level)

      # Run the model
      starttime = time()
      out = os.popen(os.environ['EXECUTABLE'] + "  ./commands.xml")
      while True:
        i = out.readline()
        if not i:
          break
        print(i.strip())
      if out.close() is not None:
        print("Planner exited abnormally")
        sys.exit(1)

      # Measure the time
      endtime = time()
      runtimes[size] = endtime - starttime
      print("%d\t%d\t%d\t%.3f" % (cluster, demand, level, runtimes[size]))

      # Clean up the files
      os.remove("input.xml")
      os.remove("output.xml")
      #if os.path.isfile("input_%d_%d_%d.xml" % (cluster,demand,level)):
      #  os.remove("input_%d_%d_%d.xml" % (cluster,demand,level))
      #if os.path.isfile("output_%d_%d_%d.xml" % (cluster,demand,level)):
      #  os.remove("output_%d_%d_%d.xml" % (cluster,demand,level))
      #os.rename("input.xml", "input_%d_%d_%d.xml" % (cluster,demand,level))
      #os.rename("output.xml", "output_%d_%d_%d.xml" % (cluster,demand,level))

# A pass criterium could be defined here.
# Right now the test pass when all cases finish successfully, and the timing
# isn't considered to evaluate pass or fail.
# Since the data import and bottleneck are the most timeconsuming operations
# a timing that scales close to linear with the model size is expected.

print("\nTest passed")
