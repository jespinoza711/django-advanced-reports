# -*- coding: utf-8 -*-
from django import forms
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.http import HttpResponse, Http404
from django.shortcuts import render_to_response, redirect
from django.template.context import RequestContext
from django.template.loader import render_to_string
from django.utils.html import strip_entities, strip_tags
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext as _
from django.db import transaction

from django_ajax.pagination import paginate

from advanced_reports import get_report_or_404
from advanced_reports.backoffice.api_utils import JSONResponse
from advanced_reports.decorators import conditional_delegation
from advanced_reports.defaults import ActionException, Resolver


def _get_redirect(advreport, next=None, querystring=None):
    if next:
        return redirect(next)
    if advreport.urlname:
        return redirect(reverse(advreport.urlname))
    suffix = u''
    if querystring:
        suffix = u'?%s' % querystring
    return redirect(reverse('advanced_reports_list', kwargs={'slug': advreport.slug}) + suffix)

@conditional_delegation(lambda request: 'delegate' in request.GET)
def list(request, slug, ids=None, internal_mode=False, report_header_visible=True):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)
    advreport.internal_mode = internal_mode
    advreport.report_header_visible = report_header_visible

    def inner(request, slug, ids):
        context = {}

        # Handle POST
        if request.method == 'POST':
            sorted_keys = [k for k in request.POST.keys()]
            sorted_keys.sort()
            selected_object_ids = [k.split('_')[2] for k in sorted_keys if 'checkbox_' in k and request.POST[k] == 'true']
            method = request.POST['method']

            if not method:
                messages.warning(request, _(u'You did not select any action.'))
                if not advreport.internal_mode:
                    return _get_redirect(advreport)

            if len(selected_object_ids) == 0:
                messages.warning(request, _(u'You did not select any %(object)s.') % {'object': advreport.verbose_name})
                if not advreport.internal_mode:
                    return _get_redirect(advreport)

            try:
                response, count = advreport.handle_multiple_actions(method, selected_object_ids, request)
                if response:
                    return response

                if count > 0:
                    messages.success(request, _(u'Successfully executed action on %(count)d %(objects)s')
                                                    % {'count': count,
                                                       'objects': advreport.verbose_name_plural if count != 1 else advreport.verbose_name})
                else:
                    messages.error(request, _(u'No selected %(object)s is applicable for this action.') % {'object': advreport.verbose_name})
                if not advreport.internal_mode:
                    return _get_redirect(advreport, querystring=request.META['QUERY_STRING'])
            except ActionException, e:
                context.update({'error': e.msg})


        object_list, extra_context = advreport.get_object_list(request, ids=ids)
        context.update(extra_context)

        # CSV?
        if 'csv' in request.GET:
            try:
                from djprogress import with_progress
            except ImportError:
                with_progress = lambda it, **kw: it
            # Avoid microsoft SYLK problem http://support.microsoft.com/kb/215591
            _mark_safe = lambda s: s if unicode(s) != u'ID' else u'"%s"' % s
            object_count = len(object_list)
            #csv = StringIO()
            header = u'%s\n' % u';'.join(_mark_safe(c['verbose_name']) for c in advreport.column_headers)
            lines = (u'%s\n' % u';'.join((c['html'] for c in o.advreport_column_values)) \
                     for o in with_progress(object_list.iterator() \
                                                if hasattr(object_list, 'iterator') \
                                                else object_list[:],
                                            name='CSV export of %s' % advreport.slug,
                                            count=object_count))
            lines = (line.replace(u'&nbsp;', u' ') for line in lines)
            lines = (line.replace(u'&euro;', u'€') for line in lines)
            lines = (line.replace(u'<br/>', u' ') for line in lines)
            lines = (strip_entities(line) for line in lines)
            lines = (strip_tags(line).encode('utf-8') for line in lines)
            #csv.write(header)
            #csv.writelines(lines)
            response = HttpResponse('', 'text/csv')
            response['Content-Disposition'] = 'attachment; filename="%s.csv"' % advreport.slug
            response.write(header)
            for line in lines:
                response.write(line)
            return response

        # Paginate
        paginated = paginate(request, object_list, num_per_page=advreport.items_per_page, use_get_parameters=True)

        # Extra context?
        context.update(advreport._extra_context(request))

        # Render
        context.update({'advreport': advreport,
                        'paginated': paginated,
                        'object_list': object_list})

        func = render_to_string if advreport.internal_mode else render_to_response
        return func(advreport.get_template(), context, context_instance=RequestContext(request))

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug, ids)

def action(request, slug, method, object_id, param=None):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug, method, object_id):
        next = request.GET.get('next', None)

        object = advreport.get_item_for_id(object_id)
        advreport.enrich_object(object, request=request)

        a = advreport.find_action(method)

        if request.method == 'POST':
            if a.form is not None:
                if issubclass(a.form, forms.ModelForm):
                    form = a.form(request.POST, request.FILES, instance=a.get_form_instance(object), prefix=object_id)
                else:
                    form = a.form(request.POST, request.FILES, prefix=object_id)

                if form.is_valid():
                    r = advreport.get_action_callable(a.method)(object, form)
                    # TODO: give feedback

                    return r or _get_redirect(advreport, next)
        else:
            if param:
                r = advreport.get_action_callable(a.method)(object, param)
            else:
                r = advreport.get_action_callable(a.method)(object)
            # TODO: give feedback

            return r or _get_redirect(advreport, next)

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug, method, object_id)


def ajax(request, slug, method, object_id, param=None):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug, method, object_id):
        object = advreport.get_item_for_id(object_id)
        advreport.enrich_object(object, request=request)
        a = advreport.find_object_action(object, method)
        if a is None:
            return HttpResponse(_(u'Unsupported action method "%s".' % method), status=404)

        context = {'advreport': advreport}

        try:
            if request.method == 'POST' and a.form is not None:
                if issubclass(a.form, forms.ModelForm):
                    form = a.form(request.POST, request.FILES, instance=a.get_form_instance(object), prefix=object_id)
                else:
                    form = a.form(request.POST, request.FILES, prefix=object_id)

                if form.is_valid():
                    advreport.get_action_callable(a.method)(object, form)
                    object = advreport.get_item_for_id(object_id)
                    context.update({'success': a.get_success_message()})
                else:
                    context.update({'response_method': method, 'response_form': form})
                    if a.form_template:
                        context.update({'response_form_template': mark_safe(render_to_string(a.form_template, {'form': form}))})

                advreport.enrich_object(object, request=request)
                context.update({'object': object})
                return render_to_response(advreport.item_template, context, context_instance=RequestContext(request))

            elif a.form is None:
                if param:
                    advreport.get_action_callable(a.method)(object, param)
                else:
                    advreport.get_action_callable(a.method)(object)
                object = advreport.get_item_for_id(object_id)
                advreport.enrich_object(object, request=request)
                context = {'object': object, 'advreport': advreport, 'success': a.get_success_message()}
                context.update({'response_method': method, 'response_form': a.form})
                if a.form_template:
                    context.update({'response_form_template': mark_safe(render_to_string(a.form_template, {'form': a.form}))})

                return render_to_response(advreport.item_template, context, context_instance=RequestContext(request))

        except ActionException, e:
            return HttpResponse(e.msg, status=404)

        # a.form is not None but not a POST request
        return HttpResponse(_(u'Unsupported request method.'), status=404)

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug, method, object_id)


def count(request, slug):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug):
        return HttpResponse(unicode(advreport.get_item_count()))

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug)


def ajax_form(request, slug, method, object_id, param=None):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug, method, object_id):
        object = advreport.get_item_for_id(object_id)
        advreport.enrich_object(object, request=request)
        a = advreport.find_object_action(object, method)
        if a is None:
            # No appropriate action found (maybe it was filtered out?)
            raise Http404

        context = {'advreport': advreport}

        if request.method == 'POST' and a.form is not None:
            if issubclass(a.form, forms.ModelForm):
                form = a.form(request.POST, request.FILES, instance=a.get_form_instance(object, param=param), prefix=object_id)
            else:
                form = a.form(request.POST, request.FILES, prefix=object_id)

            if form.is_valid():
                r = advreport.get_action_callable(a.method)(object, form)
                object = advreport.get_item_for_id(object_id)
                advreport.enrich_object(object, request=request)
                context.update({'success': a.get_success_message(), 'object': object, 'action': a})
                response = render_to_string(advreport.item_template, context, context_instance=RequestContext(request))
                return r or JSONResponse({
                    'status': 'SUCCESS',
                    'content': response
                })
            else:
                context.update({'response_method': method, 'response_form': form})
                if a.form_template:
                    context.update({'response_form_template': mark_safe(render_to_string(a.form_template, {'form': form, 'item': object}))})

            context.update({'object': object, 'action': a})
            return render_to_response('advanced_reports/ajax_form.html', context, context_instance=RequestContext(request))

        elif a.form:
            a = a.copy_with_instanced_form(prefix=object_id, instance=a.get_form_instance(advreport.get_item_for_id(object_id), param=param))
            context = {'object': object, 'advreport': advreport, 'success': a.get_success_message(), 'action': a}

            context.update({'response_method': method, 'response_form': a.form})
            if a.form_template:
                context.update({'response_form_template': mark_safe(render_to_string(a.form_template, {'form': a.form, 'item': object}))})

            return render_to_response(
                'advanced_reports/ajax_form.html',
                context,
                context_instance=RequestContext(request)
            )
        else:
            raise Http404

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug, method, object_id)


def api_form(request, slug, method, object_id):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug, method, object_id):
        object = advreport.get_item_for_id(object_id)
        advreport.enrich_object(object, request=request)
        a = advreport.find_object_action(object, method)
        if a is None or not a.form:
            # No appropriate action found (maybe it was filtered out?)
            raise Http404
        instance = a.get_form_instance(advreport.get_item_for_id(object_id))
        a = a.copy_with_instanced_form(prefix=object_id, instance=instance)

        form_instance = a.form
        rendered_form = a.form_template \
                        and render_to_string(a.form_template,
                                             {'form': form_instance, 'item': instance},
                                             context_instance=RequestContext(request)) \
            or unicode(form_instance)
        return HttpResponse(rendered_form)

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug, method, object_id)


def _action_dict(request, o, action):
    d = dict(action.attrs_dict)
    if action.form:
        if not action.prefetch_ajax_form and action.form_via_ajax:
            d['form'] = True
        else:
            form_instance = action.form
            d['form'] = action.form_template \
                            and render_to_string(action.form_template,
                                                 {'form': form_instance, 'item': o},
                                                 context_instance=RequestContext(request)) \
                or unicode(form_instance)
    if action.confirm:
        context = {'item': o}
        context.update(o.__dict__)
        d['confirm'] = action.confirm % Resolver(context)
    return d


def _item_values(request, o, advreport):
    return {
        'values': o.advreport_column_values,
        'extra_information': o.advreport_extra_information.replace('data-method="', 'ng-bind-html-unsafe="lazydiv__%s__' % advreport.get_item_id(o)),
        'actions': [_action_dict(request, o, a) for a in o.advreport_actions],
        'item_id': advreport.get_item_id(o)
    }


def _is_allowed_multiple_action(request, action):
    return not action.hidden and not action.form and action.multiple_display and action.is_allowed(request)

def api_list(request, slug, ids=None):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug, ids):
        object_list, extra_context = advreport.get_object_list(request, ids=ids)

        paginated = paginate(request, object_list, num_per_page=advreport.items_per_page, use_get_parameters=True)

        report = {
            'header': advreport.column_headers,
            'extra': extra_context,
            'items': [_item_values(request, o, advreport) for o in paginated.object_list[:]],
            'items_per_page': advreport.items_per_page,
            'item_count': len(object_list),
            'searchable_columns': advreport.searchable_columns,
            'show_action_bar': advreport.search_fields or advreport.filter_fields,
            'search_fields': advreport.search_fields,
            'filter_fields': advreport.filter_fields,
            'filter_values': advreport.filter_values,
            'action_list_type': advreport.action_list_type,
            'field_metadata': advreport.get_field_metadata_dict(),
            'report_header_visible': advreport.report_header_visible,
            'multiple_actions': advreport.multiple_actions,
            'multiple_action_list': [a.attrs_dict for a in advreport.item_actions if _is_allowed_multiple_action(request, a)]
        }
        return JSONResponse(report)

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)
    return inner(request, slug, ids)


def api_action(request, slug, method, object_id):
    advreport = get_report_or_404(slug)
    advreport.set_request(request)

    def inner(request, slug, method, object_id):
        obj = advreport.get_item_for_id(object_id)
        advreport.enrich_object(obj, request=request)
        a = advreport.find_object_action(obj, method)
        if a is None:
            return HttpResponse(_(u'Unsupported action method "%s".' % method), status=404)
        if not a.is_allowed(request):
            return HttpResponse(_(u'You\'re not allowed to execute "%s".' % method), status=404)
        context = {}
        try:
            if request.method == 'POST' and a.form is not None:
                if issubclass(a.form, forms.ModelForm):
                    form = a.form(request.POST, request.FILES, instance=a.get_form_instance(obj), prefix=object_id)
                else:
                    form = a.form(request.POST, request.FILES, prefix=object_id)

                if form.is_valid():
                    advreport.get_action_callable(a.method)(obj, form)
                    obj = advreport.get_item_for_id(object_id)
                    context.update({'success': a.get_success_message()})
                else:
                    context.update({'response_method': method, 'response_form': unicode(form)})
                    if a.form_template:
                        context.update({'response_form': render_to_string(a.form_template, {'form': form, 'item': obj})})
                if obj:
                    advreport.enrich_object(obj, request=request)
                    context.update({'item': _item_values(request, obj, advreport)})
                else:
                    context.update({'item': None, 'removed_item_id': object_id})
                return JSONResponse(context)

            elif a.form is None:
                response = advreport.get_action_callable(a.method)(obj)
                if response:
                    return response
                obj = advreport.get_item_for_id(object_id)
                if obj:
                    advreport.enrich_object(obj, request=request)
                    context = {'item': _item_values(request, obj, advreport), 'success': a.get_success_message()}
                else:
                    context = {'item': None, 'success': a.get_success_message(), 'removed_item_id': object_id}
                return JSONResponse(context)

        except ActionException, e:
            return HttpResponse(e.msg, status=404)

        # a.form is not None but not a POST request
        return HttpResponse(_(u'Unsupported request method.'), status=404)

    if advreport.decorate_views:
        inner = advreport.get_decorator()(inner)

    return inner(request, slug, method, object_id)
