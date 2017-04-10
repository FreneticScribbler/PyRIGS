import cStringIO as StringIO
from io import BytesIO
import urllib2

from django.core.mail import EmailMessage
from django.views import generic
from django.core.urlresolvers import reverse_lazy
from django.shortcuts import get_object_or_404
from django.template import RequestContext
from django.template.loader import get_template
from django.conf import settings
from django.core.urlresolvers import reverse
from django.core import signing
from django.http import HttpResponse
from django.core.exceptions import SuspiciousOperation
from django.db.models import Q
from django.contrib import messages
from z3c.rml import rml2pdf
from PyPDF2 import PdfFileMerger, PdfFileReader
import simplejson

from RIGS import models, forms
import datetime
import re
import copy

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
        context['view'] = kwargs.get('view', '')
        context['date'] = kwargs.get('date', '')
        return context


class EventDetail(generic.DetailView):
    model = models.Event


class EventOembed(generic.View):
    model = models.Event

    def get(self, request, pk=None):
        embed_url = reverse('event_embed', args=[pk])
        full_url = "{0}://{1}{2}".format(request.scheme, request.META['HTTP_HOST'], embed_url)

        data = {
            'html': '<iframe src="{0}" frameborder="0" width="100%" height="250"></iframe>'.format(full_url),
            'version': '1.0',
            'type': 'rich',
            'height': '250'
        }

        json = simplejson.JSONEncoderForHTML().encode(data)
        return HttpResponse(json, content_type="application/json")


class EventEmbed(EventDetail):
    template_name = 'RIGS/event_embed.html'


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
        old = super(EventDuplicate, self).get_object(queryset)  # Get the object (the event you're duplicating)
        new = copy.copy(old)  # Make a copy of the object in memory
        new.based_on = old  # Make the new event based on the old event
        new.purchase_order = None

        if self.request.method in (
                'POST', 'PUT'):  # This only happens on save (otherwise items won't display in editor)
            new.pk = None  # This means a new event will be created on save, and all items will be re-created
        else:
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

        merger = PdfFileMerger()

        context = RequestContext(request, {  # this should be outside the loop, but bug in 1.8.2 prevents this
            'object': object,
            'fonts': {
                'opensans': {
                    'regular': 'RIGS/static/fonts/OPENSANS-REGULAR.TTF',
                    'bold': 'RIGS/static/fonts/OPENSANS-BOLD.TTF',
                }
            },
            'quote': True,
            'current_user': request.user,
        })

        rml = template.render(context)

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


class EventArchive(generic.ArchiveIndexView):
    model = models.Event
    date_field = "start_date"
    paginate_by = 25

    def get_queryset(self):
        start = self.request.GET.get('start', None)
        end = self.request.GET.get('end', datetime.date.today())

        # Assume idiots, always check
        if start and start > end:
            messages.add_message(self.request, messages.INFO,
                                 "Muppet! Check the dates, it has been fixed for you.")
            start, end = end, start  # Stop the impending fail

        filter = False
        if end != "":
            filter = Q(start_date__lte=end)
        if start:
            if filter:
                filter = filter & Q(start_date__gte=start)
            else:
                filter = Q(start_date__gte=start)

        if filter:
            qs = self.model.objects.filter(filter).order_by('-start_date')
        else:
            qs = self.model.objects.all().order_by('-start_date')

        # Preselect related for efficiency
        qs.select_related('person', 'organisation', 'venue', 'mic')

        if len(qs) == 0:
            messages.add_message(self.request, messages.WARNING, "No events have been found matching those criteria.")

        return qs


class EventAuthorise(generic.UpdateView):
    template_name = 'RIGS/eventauthorisation_form.html'
    success_template = 'RIGS/eventauthorisation_success.html'

    def form_valid(self, form):
        self.object = form.save()

        self.template_name = self.success_template
        messages.add_message(self.request, messages.SUCCESS,
                             'Success! Your event has been authorised. You will also receive email confirmation.')
        return self.render_to_response(self.get_context_data())

    @property
    def event(self):
        return models.Event.objects.select_related('organisation', 'person', 'venue').get(pk=self.kwargs['pk'])

    def get_object(self, queryset=None):
        return getattr(self.event, 'authorisation', None)

    def get_form_class(self):
        if self.event.organisation is not None and self.event.organisation.union_account:
            return forms.InternalClientEventAuthorisationForm
        else:
            return forms.ExternalClientEventAuthorisationForm

    def get_context_data(self, **kwargs):
        context = super(EventAuthorise, self).get_context_data(**kwargs)
        context['event'] = self.event

        if self.get_form_class() is forms.InternalClientEventAuthorisationForm:
            context['internal'] = True
        else:
            context['internal'] = False

        context['tos_url'] = settings.TERMS_OF_HIRE_URL
        return context

    def get(self, request, *args, **kwargs):
        if self.get_object() is not None and self.get_object().pk is not None:
            if self.event.authorised:
                messages.add_message(self.request, messages.WARNING,
                                     "This event has already been authorised. Please confirm you wish to reauthorise")
            else:
                messages.add_message(self.request, messages.WARNING,
                                     "This event has already been authorised, but the amount has changed." +
                                     "Please check the amount and reauthorise.")
        return super(EventAuthorise, self).get(request, *args, **kwargs)

    def get_form(self, **kwargs):
        form = super(EventAuthorise, self).get_form(**kwargs)
        form.instance.event = self.event
        form.instance.email = self.request.email
        return form

    def dispatch(self, request, *args, **kwargs):
        # Verify our signature matches up and all is well with the integrity of the URL
        try:
            data = signing.loads(kwargs.get('hmac'))
            assert int(kwargs.get('pk')) == int(data.get('pk'))
            request.email = data['email']
        except (signing.BadSignature, AssertionError, KeyError):
            raise SuspiciousOperation(
                "The security integrity of that URL is invalid. Please contact your event MIC to obtain a new URL")
        return super(EventAuthorise, self).dispatch(request, *args, **kwargs)


class EventAuthorisationRequest(generic.FormView, generic.detail.SingleObjectMixin):
    model = models.Event
    form_class = forms.EventAuthorisationRequestForm
    template_name = 'RIGS/eventauthorisation_request.html'

    @property
    def object(self):
        return self.get_object()

    def get_success_url(self):
        if self.request.is_ajax():
            url = reverse_lazy('closemodal')
            messages.info(self.request, "$('.event-authorise-request').addClass('btn-success')")
        else:
            url = reverse_lazy('event_detail', kwargs={
                'pk': self.object.pk,
            })
            messages.add_message(self.request, messages.SUCCESS, "Authorisation request successfully sent.")
        return url

    def form_valid(self, form):
        email = form.cleaned_data['email']

        context = {
            'object': self.object,
            'request': self.request,
            'hmac': signing.dumps({
                'pk': self.object.pk,
                'email': email
            }),
        }

        msg = EmailMessage(
            "N%05d | %s - Event Authorisation Request".format(self.object.pk, self.object.name),
            get_template("RIGS/eventauthorisation_client_request.txt").render(context),
            to=[email],
        )

        msg.send()

        return super(EventAuthorisationRequest, self).form_valid(form)
