import cStringIO as StringIO
import copy
import datetime
import re
import urllib2
from io import BytesIO

from PyPDF2 import PdfFileMerger, PdfFileReader
from django.conf import settings
from django.contrib import messages
from django.core.urlresolvers import reverse_lazy
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template import RequestContext
from django.template.loader import get_template
from django.views import generic
from z3c.rml import rml2pdf

from RIGS import models, forms

__author__ = 'ghost'


class RigboardIndex(generic.TemplateView):
    template_name = 'RIGS/rigboard.html'

    def get_context_data(self, **kwargs):
        # get super context
        context = super(RigboardIndex, self).get_context_data(**kwargs)

        # call out method to get current events
        context['events'] = models.Event.objects.current_events()
        return context

class WebCalendar(generic.TemplateView):
    template_name = 'RIGS/calendar.html'

    def get_context_data(self, **kwargs):
        context = super(WebCalendar, self).get_context_data(**kwargs)
        context['view'] = kwargs.get('view','')
        context['date'] = kwargs.get('date','')
        return context

class EventDetail(generic.DetailView):
    model = models.Event


class EventCreate(generic.CreateView):
    model = models.Event
    form_class = forms.EventForm

    def get_context_data(self, **kwargs):
        context = super(EventCreate, self).get_context_data(**kwargs)
        context['edit'] = True
        context['currentVAT'] = models.VatRate.objects.current_rate()

        form = context['form']
        if re.search('"-\d+"', form['items_json'].value()):
            messages.info(self.request, "Your item changes have been saved. Please fix the errors and save the event.")
        

        # Get some other objects to include in the form. Used when there are errors but also nice and quick.
        for field, model in form.related_models.iteritems():
            value = form[field].value()
            if value is not None and value != '':
                context[field] = model.objects.get(pk=value)
        return context

    def get_success_url(self):
        return reverse_lazy('event_detail', kwargs={'pk': self.object.pk})


class EventUpdate(generic.UpdateView):
    model = models.Event
    form_class = forms.EventForm

    def get_context_data(self, **kwargs):
        context = super(EventUpdate, self).get_context_data(**kwargs)
        context['edit'] = True

        form = context['form']

        # Get some other objects to include in the form. Used when there are errors but also nice and quick.
        for field, model in form.related_models.iteritems():
            value = form[field].value()
            if value is not None and value != '':
                context[field] = model.objects.get(pk=value)
        return context

    def get_success_url(self):
        return reverse_lazy('event_detail', kwargs={'pk': self.object.pk})

class EventDuplicate(EventUpdate):
    def get_object(self, queryset=None):
        old = super(EventDuplicate, self).get_object(queryset) # Get the object (the event you're duplicating)
        new = copy.copy(old) # Make a copy of the object in memory
        new.based_on = old # Make the new event based on the old event

        if self.request.method in ('POST', 'PUT'): # This only happens on save (otherwise items won't display in editor)
            new.pk = None # This means a new event will be created on save, and all items will be re-created

        messages.info(self.request, 'Event data duplicated but not yet saved. Click save to complete operation.')

        return new

    def get_context_data(self, **kwargs):
        context = super(EventDuplicate, self).get_context_data(**kwargs)
        context["duplicate"] = True
        return context

class EventPrint(generic.View):
    def get(self, request, pk):
        object = get_object_or_404(models.Event, pk=pk)
        template = get_template('RIGS/event_print.xml')
        copies = ('TEC', 'Client')

        merger = PdfFileMerger()

        for copy in copies:

            context = RequestContext(request, { # this should be outside the loop, but bug in 1.8.2 prevents this
                'object': object,
                'fonts': {
                    'opensans': {
                        'regular': 'RIGS/static/fonts/OPENSANS-REGULAR.TTF',
                        'bold': 'RIGS/static/fonts/OPENSANS-BOLD.TTF',
                    }
                },
                'copy':copy,
                'current_user':request.user,
            })

            # context['copy'] = copy # this is the way to do it once we upgrade to Django 1.8.3

            rml = template.render(context)
            buffer = StringIO.StringIO()

            buffer = rml2pdf.parseString(rml)

            merger.append(PdfFileReader(buffer))

            buffer.close()

        terms = urllib2.urlopen(settings.TERMS_OF_HIRE_URL)
        merger.append(StringIO.StringIO(terms.read()))

        merged = BytesIO()
        merger.write(merged)

        response = HttpResponse(content_type='application/pdf')

        escapedEventName = re.sub('[^a-zA-Z0-9 \n\.]', '', object.name)

        response['Content-Disposition'] = "filename=N%05d | %s.pdf" % (object.pk, escapedEventName)
        response.write(merged.getvalue())
        return response

class EventArchive(generic.ListView):
    model = models.Event
    paginate_by = 25
    template_name = "RIGS/event_archive.html"

    def get_context_data(self, **kwargs):
        # get super context
        context = super(EventArchive, self).get_context_data(**kwargs)

        context['start'] = self.request.GET.get('start', None)
        context['end'] = self.request.GET.get('end', datetime.date.today().strftime('%Y-%m-%d'))
        return context

    def get_queryset(self):
        start = self.request.GET.get('start', None)
        end = self.request.GET.get('end', datetime.date.today())

        # Assume idiots, always check
        if start and start > end:
            messages.add_message(self.request, messages.INFO,
                                 "Muppet! Check the dates, it has been fixed for you.")
            start, end = end, start  # Stop the impending fail

        filter = Q()
        if end != "":
            filter &= Q(start_date__lte=end)
        if start:
            filter &= Q(start_date__gte=start)


        q = self.request.GET.get('q', "")

        if q is not "":
            qfilter = Q(name__icontains=q) | Q(description__icontains=q) | Q(notes__icontains=q)

            #try and parse an int
            try:
                val = int(q)
                qfilter = qfilter | Q(pk=val)
            except: #not an integer
                pass

            try:
                if q[0] == "N":
                    val = int(q[1:])
                    qfilter = Q(pk=val) #If string is N###### then do a simple PK filter
            except:
                pass

            filter &= qfilter

        qs = self.model.objects.filter(filter).order_by('-start_date')

        # Preselect related for efficiency
        qs.select_related('person', 'organisation', 'venue', 'mic')

        if len(qs) == 0:
            messages.add_message(self.request, messages.WARNING, "No events have been found matching those criteria.")

        return qs
