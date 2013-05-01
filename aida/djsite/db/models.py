import getpass
import os

from django.db import models as m
from django_extensions.db.fields import UUIDField
from django.contrib.auth.models import User

from django.db.models.query import QuerySet

from aida.djsite.settings.settings import LOCAL_REPOSITORY
from aida.common.exceptions import DBContentError

# Removed the custom User field, that was creating a lot of problems. Use
# the email as UUID. In case we need it, we can do a couple of south migrations
# to create the new table. See for instance
# http://stackoverflow.com/questions/14904046/

class AidaQuerySet(QuerySet):
    def iterator(self):
        for obj in super(AidaQuerySet, self).iterator():
            yield obj.get_aida_class()

class AidaObjectManager(m.Manager):
    def get_query_set(self):
        return AidaQuerySet(self.model, using=self._db)
            
class DbNode(m.Model):
    '''
    Generic node: data or calculation (or code - tbd). There will be several types of connections.
    Naming convention (NOT FINAL): A --> C --> B. 
      A is 'input' of C.
      C is 'output' of A. 
      A is 'parent' of B,C
      C,B are 'children' of A.

    FINAL DECISION:
    All attributes are stored in the Attribute table.
    * internal attributes (that are used by the Data subclass and similar) are stored
      starting with an underscore.
      These (internal) attributes cannot be changed except when defining the object
      the first time.
    * other attributes MUST start WITHOUT an underscore. These are user-defined and
      can be appended even after the calculation has run, since they just are metadata.
    * There is no json metadata attached to the DbNode entries. This can go into an attribute if needed.
    * Attributes in the Attribute table have to be thought as belonging to the DbNode,
      and this is the reason for which there is no 'user' field in the Attribute field.
    * For a Data node, attributes will /define/ the data and hence should be immutable.
      User-defined attributes are metadata for convenience of tagging and searching only.
      User should be careful not to attach data computed from data as metadata. 
    '''
    uuid = UUIDField(auto=True)
    # in the form data.upffile, data.structure, calculation, code.quantumespresso.pw, ...
    type = m.TextField(db_index=True) 
    label = m.TextField(db_index=True, blank=True)
    description = m.TextField(blank=True)
    time = m.DateTimeField(auto_now_add=True, editable=False)
    user = m.ForeignKey(User)

    # Direct links
    outputs = m.ManyToManyField('self', symmetrical=False, related_name='inputs', through='Link')  
    # Transitive closure
    children = m.ManyToManyField('self', symmetrical=False, related_name='parents', through='Path')
    
    # Used only if dbnode is a calculation
    computer = m.ForeignKey('Computer', null=True)  # only for calculations

    # Index that is incremented every time a modification is done on itself or on attributes.
    # Managed by the aida.orm.Node class. Do not modify
    nodeversion = m.IntegerField(default=1,editable=False)
    # Index that keeps track of the last time the node was updated to the remote aida instance.
    # Zero means that it was never stored, so also files need to be udpated.
    # When this number is > 0, only attributes and not files will be uploaded to the remote aida
    # instance.
    # Managed by the aida.orm.Node class. Do not modify
    lastsyncedversion = m.IntegerField(default=0,editable=False)

    objects = m.Manager()
    # Return aida Node instances or their subclasses instead of DbNode instances
    aidaobjects = AidaObjectManager()
    
    def get_aida_class(self):
        """
        Return the corresponding aida instance of class aida.orm.Node or a
        appropriate subclass.
        """
        from aida.orm import Node, Calculation, Code, Data
        from aida.common.pluginloader import load_plugin
        from aida.common import aidalogger

        thistype = self.type.split('.',1)
        baseclass = thistype[0]
        try:
            pluginclass = thistype[1]
        except IndexError:
            pluginclass = ""

        if baseclass == 'calculation':
            if pluginclass:
                try:
                    PluginClass = load_plugin(Calculation, 'aida.orm.calculationplugins', pluginclass)
                except ImportError:
                    aidalogger.warning("Unable to find calculation plugin for type '{}' (node={}), "
                                       "will use base Calculation class".format(self.type,self.uuid))
                    PluginClass = Calculation
            else:
                PluginClass = Calculation
            return PluginClass(uuid=self.uuid)
        elif baseclass == 'code':
            if pluginclass:
                try:
                    PluginClass = load_plugin(Code, 'aida.orm.codeplugins', pluginclass)
                except ImportError:
                    aidalogger.warning("Unable to find code plugin for type '{}' (node={}), "
                                       "will use base Code class".format(self.type,self.uuid))
                    PluginClass = Code
            else:
                PluginClass = Code
            return PluginClass(uuid=self.uuid)
        elif baseclass == 'data':
            if pluginclass:
                try:
                    PluginClass = load_plugin(Data, 'aida.orm.dataplugins', pluginclass)
                except ImportError:
                    aidalogger.warning("Unable to find data plugin for type '{}' (node={}), "
                                       "will use base Data class".format(self.type,self.uuid))
                    PluginClass = Data
            else:
                PluginClass = Data
            return PluginClass(uuid=self.uuid)
        else:
            if baseclass:
                # I do not print a warning for a basic node with type="".
                aidalogger.warning("Unable to find plugin for type '{}' (node={}), "
                                   "will use base Node class".format(self.type,self.uuid))
            return Node(uuid=self.uuid)
            

class Link(m.Model):
    '''
    Direct connection between two dbnodes. The label is identifying the link type.
    '''
    input = m.ForeignKey('DbNode',related_name='output_links')
    output = m.ForeignKey('DbNode',related_name='input_links')
    #label for data input for calculation
    label = m.CharField(max_length=255, db_index=True, blank=True)

    # a field to choose if this link is considered to build a transitive
    # closure or not

    # TODO: implement deletion from TC table
    # TODO: implement triggers on TC on UPDATE

    # TODO: remember to choose hierarchical names for types of dbnodes,
    #       starting with calculation. data. or code.
    # This depends on how workflow objects are implemented
    
    include_in_tc = m.BooleanField()

    
    # TODO: I also want to check some more logic e.g. to avoid having
    #       two calculations as input of a data object.
    def save(self,*args,**kwargs):
        if self.pk is None:
            if ((self.input.type.startswith('calculation') or
                 self.input.type.startswith('data')) and
                (self.output.type.startswith('calculation') or
                 self.output.type.startswith('data'))):
                self.include_in_tc = True
            else:
                self.include_in_tc = False
        super(Link,self).save(*args,**kwargs)

class Path(m.Model):
    """
    Transitive closure table for all dbnode paths.
    """
    parent = m.ForeignKey('DbNode',related_name='child_paths',editable=False)
    child = m.ForeignKey('DbNode',related_name='parent_paths',editable=False)
    depth = m.IntegerField(editable=False)

    # Used to delete
    entry_edge_id = m.IntegerField(null=True,editable=False)
    direct_edge_id = m.IntegerField(null=True,editable=False)
    exit_edge_id = m.IntegerField(null=True,editable=False)


attrdatatype_choice = (
    ('float', 'float'),
    ('int', 'int'),
    ('txt', 'txt'),
    ('bool', 'bool'),
    ('json', 'json'))

class Attribute(m.Model):
    '''
    Attributes are annotations ONLY for storing metadata and tagging. This is only for
    querying convenience.
    Actual input and output data should never go here, only duplicates and comments.
    '''
    time = m.DateTimeField(auto_now_add=True, editable=False)
    dbnode = m.ForeignKey('DbNode', related_name='attributes')
    key = m.TextField(db_index=True)
    datatype = m.CharField(max_length=10, choices=attrdatatype_choice, db_index=True)
    tval = m.TextField( default='', blank=True)
    fval = m.FloatField( default=None, null=True)
    ival = m.IntegerField( default=None, null=True)
    bval = m.NullBooleanField( default=None, null=True)

    class Meta:
        unique_together = (("dbnode", "key"))
        
    def setvalue(self,value):
        """
        This can be called on a given row and will set the corresponding value.
        NOTE: Rules below need to be checked.
        """
        import json
        if isinstance(value,bool):
            self.datatype = 'bool'
            self.bval = value
            self.tval = ''
            self.ival = None
            self.fval = None

        elif isinstance(value,int):
            self.datatype = 'int'
            self.ival = value
            self.tval = ''
            self.bval = None
            self.fval = None

        elif isinstance(value,float):
            self.datatype = 'float'
            self.fval = value
            self.tval = ''
            self.ival = None
            self.bval = None

        elif isinstance(value,basestring):
            self.datatype = 'txt'
            self.tval = value
            self.bval = None
            self.ival = None
            self.fval = None

        else:
            try:
                jsondata = json.dumps(value)
            except TypeError:
                raise ValueError("Unable to store the value: it must be either "
                                 "a basic datatype, or json-serializable")
            
            self.datatype = 'json'
            self.tval = jsondata
            self.bval = None
            self.ival = None
            self.fval = None
        
        self.save()       
        
    def getvalue(self):
        """
        This can be called on a given row and will get the corresponding value,
        casting it correctly
        """
        import json
        if self.datatype == 'bool':
            return self.bval
        elif self.datatype == 'int':
            return self.ival
        elif self.datatype == 'float':
            return self.fval
        elif self.datatype == 'txt':
            return self.tval
        elif self.datatype == 'json':
            try:
                return json.loads(self.tval)
            except ValueError:
                raise DBContentError("Error in the content of the json field")
        else:
            raise DBContentError("The type field '{}' is not recognized".format(
                    self.datatype))        


class Group(m.Model):
    uuid = UUIDField(auto=True)
    name = m.TextField(unique=True, db_index=True)
    dbnodes = m.ManyToManyField('DbNode', related_name='groups')
    time = m.DateTimeField(auto_now_add=True, editable=False)
    description = m.TextField(blank=True)
    user = m.ForeignKey(User)  # The owner of the group, not of the calculations

class Computer(m.Model):
    """Table of computers or clusters.

    # TODO: understand if we want that this becomes simply another type of dbnode.

    Attributes:
        hostname: Full hostname of the host
        workdir: Full path of the aida folder on the host. It can contain
            the string {username} that will be substituted by the username
            of the user on that machine.
            The actual workdir is then obtained as
            workdir.format(username=THE_ACTUAL_USERNAME)
            Example: 
            workdir = "/scratch/{username}/aida/"
        transport_type: a string with a valid transport type


    Note: other things that may be set in the metadata:
        - mpirun command
        - num cores per node
        - max num cores
        - allocate full node = True or False
        - ... (further limits per user etc.)
    """
    uuid = UUIDField(auto=True)
    hostname = m.CharField(max_length=255, unique=True) # FQDN
    description = m.TextField(blank=True)
    workdir = m.CharField(max_length=255)
    transport_type = m.CharField(max_length=255)
    scheduler_type = m.CharField(max_length=255)
    transport_params = m.TextField(default="{}") # Will store a json

    metadata = m.TextField(default="{}") # Will store a json

    def get_transport_params(self):
        import json
        try:
            return json.loads(self.transport_params)
        except ValueError:
            raise DBContentError(
                "Error while reading transport_params for computer {}".format(
                    self.hostname))
        
    def get_scheduler(self):
        import aida.scheduler
        from aida.common.pluginloader import load_plugin

        try:
            ThisPlugin = load_plugin(aida.scheduler.Scheduler, 'aida.scheduler.plugins',
                               self.scheduler_type)
            # I call the init without any parameter
            return ThisPlugin()
        except ImportError as e:
            raise ConfigurationError('No scheduler found for {} [type {}], message: {}'.format(
                self.hostname, self.scheduler_type, e.message))

#class RunningJob(m.Model):
#    calc = m.OneToOneField(DbNode,related_name='jobinfo') # OneToOneField implicitly sets unique=True
#    calc_state = m.CharField(max_length=64)
#    job_id = m.TextField(blank=True)
#    scheduler_state = m.CharField(max_length=64,blank=True)
#    # Will store a json of the last JobInfo got from the scheduler
#    last_jobinfo = m.TextField(default='{}')  

class AuthInfo(m.Model):
    """
    Table that pairs aida users and computers, with all required authentication
    information.
    """
    aidauser = m.ForeignKey(User)
    computer = m.ForeignKey(Computer)
    auth_params = m.TextField(default='{}')  # Will store a json; contains mainly the remoteuser
                                             # and the private_key

    class Meta:
        unique_together = (("aidauser", "computer"),)

    def update_calc_states(self):
        import aida
        aida.execmanager.update_calc_states(self)

    def get_auth_params(self):
        import json
        try:
            return json.loads(self.auth_params)
        except ValueError:
            raise DBContentError(
                "Error while reading auth_params for authinfo, aidauser={}, computer={}".format(
                    self.aidauser, self.computer))

    # a method of AuthInfo
    def get_transport(self):
        """
        Given a computer and an aida user (as entries of the DB) return a configured
        transport to connect to the computer.
        """    
        from aida.common.pluginloader import load_plugin

        try:
            ThisTransport = load_plugin(aida.transport.Transport, 'aida.transport.plugins',
                                        self.computer.transport_type)
        except ImportError as e:
            raise ConfigurationError('No transport found for {} [type {}], message: {}'.format(
                self.computer.hostname, self.computer.transport_type, e.message))

        params = self.computer.get_transport_params() + self.get_auth_params()
        return ThisTransport(machine=self.computer.hostname,**params)

class Comment(m.Model):
    dbnode = m.ForeignKey(DbNode,related_name='comments')
    time = m.DateTimeField(auto_now_add=True, editable=False)
    user = m.ForeignKey(User)
    content = m.TextField(blank=True)


#
#
#

workflow_step_exit = "wf_exit"

class DbWorkflow(m.Model):

    from aida.common.datastructures import calcStates

    uuid        = UUIDField(auto=True)
    time        = m.DateTimeField(auto_now_add=True, editable=False)
    user        = m.ForeignKey(User)
    comment     = m.TextField(blank=True)
    status      = m.CharField(choices=calcStates,default=calcStates.NEW)

    def get_calculations(self):

        return Calculations.query(steps=self.steps)

    
class DbWorkflowStep(m.Model):

    from aida.common.datastructures import calcStates

    workflow     = m.ForeignKey(DbWorkflow, related_name='steps')
    name         = m.CharField(max_length=255, blank=False)
    user         = m.ForeignKey(User)
    time         = m.DateTimeField(auto_now_add=True, editable=False)
    nextcall     = m.CharField(max_length=255, blank=False, default=workflow_step_exit)
    calculations = m.ManyToManyField('DbNode', symmetrical=False, related_name="steps")
    status       = m.CharField(choices=calcStates,default=calcStates.NEW)

    class Meta:
        unique_together = (("workflow", "name"))


    def add_calculation(self, calculation):
        
        from aida.orm import Calculation

        if (not isinstance(step_calculation, Calculation)):
            raise ValueError("Cannot add a non-Calculation object to a workflow step")          

        self.calculations.add(calculation)

    def get_calculations(self):

        return Calculations.query(steps=self)#pk__in = step.calculations.values_list("pk", flat=True))

    def update_status(self, extended = False):

        calc_status = self.calculations.filter(attributes__key="_state").values_list("uuid", "attributes__tval")
        s = set([l[1] for l in status])
        
        return