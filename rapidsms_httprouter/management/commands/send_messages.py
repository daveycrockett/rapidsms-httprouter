import traceback
import time
from django.core.management.base import BaseCommand
from rapidsms.models import Backend, Connection, Contact
from rapidsms_httprouter.models import Message, MessageBatch
from rapidsms_httprouter.router import get_router
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction, close_connection
from urllib import quote_plus
from urllib2 import urlopen
from rapidsms.log.mixin import LoggerMixin

class Command(BaseCommand, LoggerMixin):

    help = """sends messages from all project DBs
    """

    def fetch_url(self, url):
        """
        Wrapper around url open, mostly here so we can monkey patch over it in unit tests.
        """
        response = urlopen(url, timeout=15)
        return response.getcode()


    def build_send_url(self, router_url, backend, recipients, text, **kwargs):
        """
        Constructs an appropriate send url for the given message.
        """
        # first build up our list of parameters
        params = {
            'backend': backend,
            'recipient': recipients,
            'text': text,
        }

        # make sure our parameters are URL encoded
        params.update(kwargs)
        for k, v in params.items():
            try:
                params[k] = quote_plus(str(v))
            except UnicodeEncodeError:
                params[k] = quote_plus(str(v.encode('UTF-8')))

        # is this actually a dict?  if so, we want to look up the appropriate backend
        if type(router_url) is dict:
            router_dict = router_url
            backend_name = backend

            # is there an entry for this backend?
            if backend_name in router_dict:
                router_url = router_dict[backend_name]

            # if not, look for a default backend
            elif 'default' in router_dict:
                router_url = router_dict['default']

            # none?  blow the hell up
            else:
                self.error("No router url mapping found for backend '%s', check your settings.ROUTER_URL setting" % backend_name)
                raise Exception("No router url mapping found for backend '%s', check your settings.ROUTER_URL setting" % backend_name)

        # return our built up url with all our variables substituted in
        full_url = router_url % params

        return full_url


    def send_backend_chunk(self, router_url, pks, backend_name):
        msgs = Message.objects.using(self.db).filter(pk__in=pks).exclude(connection__identity__iregex="[a-z]")
        try:
            url = self.build_send_url(router_url, backend_name, ','.join(msgs.values_list('connection__identity', flat=True)), msgs[0].text)
            status_code = self.fetch_url(url)

            # kannel likes to send 202 responses, really any
            # 2xx value means things went okay
            if int(status_code / 100) == 2:
                self.info("SMS%s SENT" % pks)
                msgs.update(status='S')
            else:
                self.info("SMS%s Message not sent, got status: %s .. queued for later delivery." % (pks, status_code))
                msgs.update(status='Q')

        except Exception as e:
            self.error("SMS%s Message not sent: %s .. queued for later delivery." % (pks, str(e)))
            msgs.update(status='Q')


    def send_all(self, router_url, to_send):
        pks = []
        if len(to_send):
            backend_name = to_send[0].connection.backend.name
            for msg in to_send:
                if backend_name != msg.connection.backend.name:
                    # send all of the same backend
                    self.send_backend_chunk(router_url, pks, backend_name)
                    # reset the loop status variables to build the next chunk of messages with the same backend
                    backend_name = msg.connection.backend.name
                    pks = [msg.pk]
                else:
                    pks.append(msg.pk)
            self.send_backend_chunk(router_url, pks, backend_name)

    def send_individual(self, router_url):
        to_process = Message.objects.using(self.db).filter(direction='O',
                          status__in=['Q']).order_by('priority', 'status', 'connection__backend__name')
        if len(to_process):
            self.send_all(router_url, [to_process[0]])


    def handle(self, **options):
        """

        """
        DBS = settings.DATABASES.keys()
        # DBS.remove('default') # skip the dummy -we now check default DB as well
        CHUNK_SIZE = getattr(settings, 'MESSAGE_CHUNK_SIZE', 400)
        self.info("starting up")
        recipients = getattr(settings, 'ADMINS', None)
        if recipients:
            recipients = [email for name, email in recipients]
        while (True):
            self.debug("entering main loop")
            for db in DBS:
                try:
                    self.debug("servicing db '%s'" % db)
                    router_url = settings.DATABASES[db]['ROUTER_URL']
                    transaction.enter_transaction_management(using=db)
                    self.db = db
                    to_process = MessageBatch.objects.using(db).filter(status='Q')
                    self.debug("looking for batch messages to process")
                    if to_process.count():
                        self.info("found %d batches in %s to process" % (to_process.count(), db))
                        batch = to_process[0]
                        to_process = batch.messages.using(db).filter(direction='O',
                                      status__in=['Q']).order_by('priority', 'status', 'connection__backend__name')[:CHUNK_SIZE]
                        self.info("%d chunk of messages found in %s" % (to_process.count(), db))
                        if to_process.count():
                            self.debug("found batch message %d with Queued messages to send" % batch.pk)
                            self.send_all(router_url, to_process)
                        elif batch.messages.using(db).filter(status__in=['S', 'C']).count() == batch.messages.using(db).count():
                            self.info("found batch message %d ready to be closed" % batch.pk)
                            batch.status = 'S'
                            batch.save()
                        else:
                            self.debug("reverting to individual message sending")
                            self.send_individual(router_url)
                    else:
                        self.debug("no batches found, reverting to individual message sending")
                        self.send_individual(router_url)
                    transaction.commit(using=db)
                except Exception, exc:
                    transaction.rollback(using=db)
                    print self.critical(traceback.format_exc(exc))
                    if recipients:
                        send_mail('[Django] Error: messenger command', str(traceback.format_exc(exc)), 'root@uganda.rapidsms.org', recipients, fail_silently=True)
                    continue

            # yield from the messages table, messenger can cause
            # deadlocks if it's contanstly polling the messages table
            close_connection()
            time.sleep(0.5)



