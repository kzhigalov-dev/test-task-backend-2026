from amocrm import Customer, Lead


def get_customer_json(customer: Customer) -> dict:
    return {
        'general': {
            'id': customer.id,
            'login': customer.custom_fields_values.get('Логин', {}).get('value'),
            'billing_type': customer.custom_fields_values.get('Вид оплаты', {}).get('value'),
            'is_robot': bool(customer.custom_fields_values.get('Робот')),
            'is_nds': bool(customer.custom_fields_values.get('Оплата с НДС')),
            'system': customer.custom_fields_values.get('Система', {}).get('value'),
            'host': customer.custom_fields_values.get('Хост', {}).get('value'),
            'name': customer.name,
        },
        'api_value': customer.as_dict()
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        'general': {
            'id': lead.id,
            'login': lead.custom_fields_values.get('Логин', {}).get('value'),
            'name': lead.name,
        },
        'api_value': lead.as_dict()
    }
