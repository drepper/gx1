Gira X1 Controller
==================

This projects is about libraries and modules to help writing programs to control Gira X1 systems.

The first implementation will use Python.  A Python module will provide a class definition which
can be instantiated with the connection details of the X1 and which then can be used to control
and query all the information needed.


If details of the protocol are not known it is possible to capture traffic of a working system.
To do this, create a program running on the local machine to act as a proxy.  If just also just
need the connection details of the X1 server.

None of the connection details must be stored in the repository.  Read this information from a
configuration file which is never committed to the repository.
