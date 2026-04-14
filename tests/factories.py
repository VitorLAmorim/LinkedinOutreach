# tests/factories.py
import factory
from faker import Faker

fake = Faker()


class LinkedInAccountFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "linkedin.LinkedInAccount"

    username = factory.LazyFunction(fake.user_name)
    linkedin_username = factory.LazyFunction(lambda: fake.email())
    linkedin_password = factory.LazyFunction(lambda: fake.password())
    active = True


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "crm.Lead"

    first_name = factory.LazyFunction(fake.first_name)
    last_name = factory.LazyFunction(fake.last_name)
    linkedin_url = factory.LazyFunction(
        lambda: f"https://www.linkedin.com/in/{fake.user_name()}/"
    )


class DealFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "crm.Deal"

    lead = factory.SubFactory(LeadFactory)
